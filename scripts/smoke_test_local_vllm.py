"""Smoke test MiniTen with a real vLLM container on local Kubernetes."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from typing import Any

from smoke_test_local_api import (
    PASSWORD,
    SmokeClient,
    ensure_api_is_running,
    wait_for_deployment_job,
)
from smoke_test_local_k8s import (
    cleanup,
    get,
    has_hf_token,
    port_forward,
    run_kubectl,
    verify_deleted_resources,
    verify_kubectl_available,
)


DEFAULT_VLLM_IMAGE = "vllm/vllm-openai:latest"
DEFAULT_VLLM_CPU_IMAGE = "vllm/vllm-openai-cpu:latest-x86_64"
DEFAULT_MODEL_ID = "HuggingFaceTB/SmolLM2-135M-Instruct"
DEFAULT_PORT_FORWARD_PORT = 18080
DEFAULT_JOB_TIMEOUT_SECONDS = 1800


def run_real_vllm_smoke_test(
    *,
    base_url: str,
    model_id: str,
    port_forward_port: int,
) -> None:
    """Deploy real vLLM, call inference, verify analytics, and clean up."""
    client = SmokeClient(base_url)
    suffix = uuid.uuid4().hex[:8]
    email = f"vllm-smoke-{suffix}@example.com"
    model_name = f"vllm-smoke-{suffix}"
    token = None
    project_id = None
    model_deployment_id = None
    namespace = None
    deployment: dict[str, Any] | None = None

    ensure_api_is_running(client)
    verify_kubectl_available()
    gpu_count = int(os.getenv("MINITEN_VLLM_TEST_GPU_COUNT", "0"))
    expected_vllm_image = expected_managed_vllm_image(gpu_count)
    print(f"Expecting MiniTen-managed vLLM image: {expected_vllm_image}")
    print(f"Using Hugging Face model: {model_id}")
    print(f"Using vLLM Kubernetes device: {os.getenv('MINITEN_VLLM_TEST_DEVICE', 'cpu')}")

    try:
        client.request(
            "POST",
            "/v1/users",
            json={"email": email, "password": PASSWORD},
            expected_status=201,
        )
        login = client.request(
            "POST",
            "/v1/auth/login",
            json={"email": email, "password": PASSWORD},
        )
        token = login["access_token"]
        project = client.request(
            "POST",
            "/v1/projects",
            token=token,
            json={"name": f"vLLM Smoke {suffix}"},
            expected_status=201,
        )
        project_id = project["projectID"]
        namespace = project["k8s_namespace"]

        deploy = client.request(
            "POST",
            f"/v1/projects/{project_id}/models",
            token=token,
            json={
                "name": model_name,
                "model_id": model_id,
                "replicas": 1,
                "resources": {
                    "cpu_request": os.getenv("MINITEN_VLLM_TEST_CPU_REQUEST", "2"),
                    "cpu_limit": os.getenv("MINITEN_VLLM_TEST_CPU_LIMIT", "4"),
                    "memory_request": os.getenv("MINITEN_VLLM_TEST_MEMORY_REQUEST", "4Gi"),
                    "memory_limit": os.getenv("MINITEN_VLLM_TEST_MEMORY_LIMIT", "12Gi"),
                    "gpu_count": gpu_count,
                },
                "vllm": {
                    "dtype": os.getenv("MINITEN_VLLM_TEST_DTYPE", "auto"),
                    "max_model_len": int(os.getenv("MINITEN_VLLM_TEST_MAX_MODEL_LEN", "512")),
                },
                "autoscaling": {
                    "enabled": False,
                },
            },
            expected_status=201,
        )
        deployment = deploy["modelDeployment"]
        if deployment["vllm"]["image"] != expected_vllm_image:
            raise RuntimeError(
                "API selected unexpected vLLM image "
                f"{deployment['vllm']['image']}; expected {expected_vllm_image}."
            )
        model_deployment_id = deployment["modelDeploymentID"]
        wait_for_deployment_job(
            client,
            project_id,
            model_deployment_id,
            deploy["deploymentJob"]["deploymentJobID"],
            token,
            max_attempts=int(os.getenv("MINITEN_VLLM_TEST_JOB_POLLS", "1800")),
        )

        verify_created_vllm_resources(namespace, deployment, expect_secret=has_hf_token())
        api_key_response = client.request(
            "POST",
            f"/v1/projects/{project_id}/api-keys",
            token=token,
            json={"name": f"vLLM Smoke Key {suffix}"},
            expected_status=201,
        )
        api_key = api_key_response["api_key"]

        with port_forward(namespace, deployment["k8s_service_name"], port_forward_port):
            response = client.request(
                "POST",
                "/v1/chat/completions",
                project_api_key=api_key,
                json={
                    "model": model_name,
                    "messages": [{"role": "user", "content": "Say hello in two words."}],
                    "max_tokens": 8,
                    "temperature": 0,
                },
                expected_status=200,
            )
            if not response.get("choices"):
                raise RuntimeError(f"Expected vLLM choices in response: {response}")
            print(f"vLLM inference response id: {response.get('id')}")
            print(f"vLLM assistant output: {extract_assistant_output(response)}")

        metrics = client.request(
            "GET",
            f"/v1/projects/{project_id}/analytics/models/{model_name}/metrics",
            token=token,
        )
        if metrics["metrics"]["request_count"] < 1:
            raise RuntimeError("Expected analytics metrics to include vLLM inference.")

        requests_body = client.request(
            "GET",
            f"/v1/projects/{project_id}/analytics/models/{model_name}/requests",
            token=token,
        )
        if not requests_body["requests"]:
            raise RuntimeError("Expected request history to include vLLM inference.")

        delete = client.request(
            "DELETE",
            f"/v1/projects/{project_id}/models/{model_deployment_id}",
            token=token,
            expected_status=202,
        )
        wait_for_deployment_job(
            client,
            project_id,
            model_deployment_id,
            delete["deploymentJob"]["deploymentJobID"],
            token,
        )
        verify_deleted_resources(namespace, deployment, expect_secret=has_hf_token())
        print(f"Real vLLM smoke test passed for namespace {namespace}.")
    except Exception:
        if namespace:
            print_kubernetes_diagnostics(namespace)
        raise
    finally:
        cleanup(client, token, project_id, model_deployment_id, namespace)


def verify_created_vllm_resources(
    namespace: str,
    deployment: dict[str, Any],
    *,
    expect_secret: bool,
) -> None:
    """Assert the real vLLM worker created expected Kubernetes resources."""
    get("namespace", namespace)
    get("pvc", f"{deployment['name']}-hf-cache", namespace)
    get("deployment", deployment["k8s_deployment_name"], namespace)
    get("service", deployment["k8s_service_name"], namespace)
    if expect_secret:
        get("secret", f"{deployment['name']}-secrets", namespace)


def expected_managed_vllm_image(gpu_count: int) -> str:
    """Return the image MiniTen should choose for the requested GPU count."""
    if gpu_count == 0:
        return os.getenv("VLLM_CPU_IMAGE", DEFAULT_VLLM_CPU_IMAGE)
    return os.getenv("VLLM_IMAGE", DEFAULT_VLLM_IMAGE)


def extract_assistant_output(response: dict[str, Any]) -> str:
    """Return the assistant text from a vLLM chat-completions response."""
    message = response["choices"][0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    return str(content)


def print_kubernetes_diagnostics(namespace: str) -> None:
    """Print pod, event, describe, and log diagnostics for failed vLLM smoke tests."""
    print("\n--- Kubernetes diagnostics ---", file=sys.stderr)
    for args in [
        ["get", "pods", "-n", namespace, "-o", "wide"],
        [
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/name=miniten",
            "-o",
            "jsonpath={.items[0].spec.containers[0].env}",
        ],
        [
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/name=miniten",
            "-o",
            "jsonpath={.items[0].spec.containers[0].volumeMounts}",
        ],
        [
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/name=miniten",
            "-o",
            "jsonpath={.items[0].spec.volumes}",
        ],
        ["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"],
        ["describe", "pods", "-n", namespace],
        ["logs", "-n", namespace, "-l", "app.kubernetes.io/name=miniten", "--tail=100"],
        [
            "logs",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/name=miniten",
            "--previous",
            "--tail=200",
        ],
    ]:
        result = run_kubectl(args, check=False)
        print(f"$ kubectl {' '.join(args)}", file=sys.stderr)
        print(result.stdout or result.stderr, file=sys.stderr)


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(
        description="Smoke test MiniTen with a real vLLM deployment.",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--model-id",
        default=os.getenv("MINITEN_VLLM_TEST_MODEL_ID", DEFAULT_MODEL_ID),
    )
    parser.add_argument(
        "--port-forward-port",
        default=int(os.getenv("MINITEN_VLLM_TEST_PORT_FORWARD_PORT", DEFAULT_PORT_FORWARD_PORT)),
        type=int,
    )
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()
    try:
        run_real_vllm_smoke_test(
            base_url=args.base_url,
            model_id=args.model_id,
            port_forward_port=args.port_forward_port,
        )
    except Exception as exc:
        print(f"Real vLLM smoke test failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
