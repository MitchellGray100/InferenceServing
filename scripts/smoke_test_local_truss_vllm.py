"""Smoke test MiniTen real vLLM deployment through the Truss CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
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
    verify_deleted_resources,
    verify_kubectl_available,
)
from smoke_test_local_vllm import (
    DEFAULT_MODEL_ID,
    DEFAULT_PORT_FORWARD_PORT,
    expected_managed_vllm_image,
    extract_assistant_output,
    print_kubernetes_diagnostics,
)


def run_truss_vllm_smoke_test(
    *,
    base_url: str,
    model_id: str,
    port_forward_port: int,
) -> None:
    """Deploy real vLLM using Truss commands, call inference, and clean up."""
    client = SmokeClient(base_url)
    suffix = uuid.uuid4().hex[:8]
    email = f"truss-vllm-smoke-{suffix}@example.com"
    project_name = f"truss-vllm-smoke-{suffix}"
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

    with tempfile.TemporaryDirectory(prefix="miniten-truss-smoke-") as tmp:
        tmp_path = Path(tmp)
        truss_env = os.environ.copy()
        truss_env["MINITEN_CLI_CONFIG"] = str(tmp_path / "miniten-config.json")

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
            account_key_response = client.request(
                "POST",
                "/v1/account/api-keys",
                token=token,
                json={"name": f"Truss Smoke Account Key {suffix}"},
                expected_status=201,
            )
            account_api_key = account_key_response["api_key"]

            run_truss(
                ["login", "--api-key", account_api_key, "--base-url", base_url],
                cwd=tmp_path,
                env=truss_env,
            )
            run_truss(
                ["init", project_name, "--model-name", model_name],
                cwd=tmp_path,
                env=truss_env,
            )

            project_dir = tmp_path / project_name
            write_truss_config(
                project_dir / "config.yaml",
                model_name=model_name,
                model_id=model_id,
                gpu_count=gpu_count,
            )
            run_truss(["push", "--no-watch"], cwd=project_dir, env=truss_env)

            project = find_project_by_name(client, token, project_name)
            project_id = project["projectID"]
            namespace = project["k8s_namespace"]
            deployment = find_model_by_name(client, token, project_id, model_name)
            if deployment["vllm"]["image"] != expected_vllm_image:
                raise RuntimeError(
                    "Truss deploy selected unexpected vLLM image "
                    f"{deployment['vllm']['image']}; expected {expected_vllm_image}."
                )
            model_deployment_id = deployment["modelDeploymentID"]
            jobs = client.request(
                "GET",
                f"/v1/projects/{project_id}/models/{model_deployment_id}/jobs",
                token=token,
            )
            deploy_job = newest_job(jobs["deploymentJobs"], "deploy_model")
            wait_for_deployment_job(
                client,
                project_id,
                model_deployment_id,
                deploy_job["deploymentJobID"],
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
                        "messages": [
                            {"role": "user", "content": "Say hello in two words."}
                        ],
                        "max_tokens": 8,
                        "temperature": 0,
                    },
                    expected_status=200,
                )
                if not response.get("choices"):
                    raise RuntimeError(f"Expected vLLM choices in response: {response}")
                print(f"Truss vLLM inference response id: {response.get('id')}")
                print(f"Truss vLLM assistant output: {extract_assistant_output(response)}")

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
            print(f"Truss vLLM smoke test passed for namespace {namespace}.")
        except Exception:
            if namespace:
                print_kubernetes_diagnostics(namespace)
            raise
        finally:
            cleanup(client, token, project_id, model_deployment_id, namespace)


def run_truss(args: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    """Run the local Truss CLI and fail with captured output on error."""
    command = [sys.executable, "-m", "app.truss_cli", *args]
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    print(f"$ {' '.join(command)}")
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"truss {' '.join(args)} failed with exit code {result.returncode}"
        )


def write_truss_config(
    path: Path,
    *,
    model_name: str,
    model_id: str,
    gpu_count: int,
) -> None:
    """Write the model config used by `truss push`."""
    path.write_text(
        "\n".join(
            [
                f"model_name: {model_name}",
                f"model_id: {model_id}",
                "replicas: 1",
                "",
                "resources:",
                f'  cpu_request: "{os.getenv("MINITEN_VLLM_TEST_CPU_REQUEST", "2")}"',
                f'  cpu_limit: "{os.getenv("MINITEN_VLLM_TEST_CPU_LIMIT", "4")}"',
                (
                    "  memory_request: "
                    f'"{os.getenv("MINITEN_VLLM_TEST_MEMORY_REQUEST", "4Gi")}"'
                ),
                (
                    "  memory_limit: "
                    f'"{os.getenv("MINITEN_VLLM_TEST_MEMORY_LIMIT", "12Gi")}"'
                ),
                f"  gpu_count: {gpu_count}",
                "",
                "vllm:",
                f'  dtype: {os.getenv("MINITEN_VLLM_TEST_DTYPE", "auto")}',
                (
                    "  max_model_len: "
                    f'{int(os.getenv("MINITEN_VLLM_TEST_MAX_MODEL_LEN", "512"))}'
                ),
                "",
                "autoscaling:",
                "  enabled: false",
                "",
            ]
        ),
        encoding="utf-8",
    )


def find_project_by_name(
    client: SmokeClient,
    token: str,
    name: str,
) -> dict[str, Any]:
    """Return a project by name from the authenticated user's project list."""
    projects = client.request("GET", "/v1/projects", token=token)["projects"]
    for project in projects:
        if project["name"] == name:
            return project
    raise RuntimeError(f"Truss project {name!r} was not found after init.")


def find_model_by_name(
    client: SmokeClient,
    token: str,
    project_id: str,
    name: str,
) -> dict[str, Any]:
    """Return a model deployment by name."""
    models = client.request("GET", f"/v1/projects/{project_id}/models", token=token)[
        "modelDeployments"
    ]
    for model in models:
        if model["name"] == name:
            return model
    raise RuntimeError(f"Truss model {name!r} was not found after push.")


def newest_job(jobs: list[dict[str, Any]], job_type: str) -> dict[str, Any]:
    """Return the newest job matching a job type."""
    for job in jobs:
        if job["job_type"] == job_type:
            return job
    raise RuntimeError(f"Expected a {job_type} deployment job.")


def verify_created_vllm_resources(
    namespace: str,
    deployment: dict[str, Any],
    *,
    expect_secret: bool,
) -> None:
    """Assert the Truss vLLM worker created expected Kubernetes resources."""
    get("namespace", namespace)
    get("pvc", f"{deployment['name']}-hf-cache", namespace)
    get("deployment", deployment["k8s_deployment_name"], namespace)
    get("service", deployment["k8s_service_name"], namespace)
    if expect_secret:
        get("secret", f"{deployment['name']}-secrets", namespace)


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(
        description="Smoke test MiniTen real vLLM deployment through Truss.",
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
        run_truss_vllm_smoke_test(
            base_url=args.base_url,
            model_id=args.model_id,
            port_forward_port=args.port_forward_port,
        )
    except Exception as exc:
        print(f"Truss vLLM smoke test failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
