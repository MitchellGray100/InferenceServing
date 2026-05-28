"""Smoke test MiniTen against a real local Kubernetes worker.

This script assumes:
- `make setup-env` has already started Postgres.
- `make run-api` is running in another terminal.
- `make start-worker-real-k8s` started the Docker Compose worker with access to
  a local kind/minikube kubeconfig.
- `kubectl` can talk to the same cluster.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from typing import Any

from smoke_test_local_api import (
    PASSWORD,
    SmokeClient,
    ensure_api_is_running,
    wait_for_deployment_job,
)


DEFAULT_MODEL_ID = "facebook/opt-125m"
DEFAULT_SMOKE_IMAGE = "hashicorp/http-echo:1.0"


def run_real_k8s_smoke_test(base_url: str, model_id: str, smoke_image: str) -> None:
    """Create, verify, and delete one real Kubernetes-backed model deployment."""
    client = SmokeClient(base_url)
    suffix = uuid.uuid4().hex[:8]
    email = f"k8s-smoke-{suffix}@example.com"
    model_name = f"k8s-smoke-{suffix}"
    token = None
    project_id = None
    model_deployment_id = None
    namespace = None

    ensure_api_is_running(client)
    verify_kubectl_available()
    print(f"Using Kubernetes smoke image: {smoke_image}")

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
            json={"name": f"K8s Smoke {suffix}"},
            expected_status=201,
        )
        project_id = project["projectID"]
        namespace = project["k8s_namespace"]

        deploy = client.request(
            "POST",
            f"/v1/projects/{project_id}/models",
            token=token,
            headers={"Idempotency-Key": f"deploy-{suffix}"},
            json={
                "name": model_name,
                "model_id": model_id,
                "replicas": 1,
                "resources": {
                    "cpu_request": "50m",
                    "cpu_limit": "250m",
                    "memory_request": "64Mi",
                    "memory_limit": "256Mi",
                    "gpu_count": 0,
                },
                "vllm": {
                    "image": smoke_image,
                    "dtype": "auto",
                    "max_model_len": 128,
                },
                "autoscaling": {
                    "enabled": True,
                    "min_replicas": 1,
                    "max_replicas": 1,
                    "target_cpu_utilization": 70,
                },
            },
            expected_status=201,
        )
        deployment = deploy["modelDeployment"]
        print(f"Created model deployment with image: {deployment['vllm']['image']}")
        model_deployment_id = deployment["modelDeploymentID"]
        wait_for_deployment_job(
            client,
            project_id,
            model_deployment_id,
            deploy["deploymentJob"]["deploymentJobID"],
            token,
        )

        verify_created_resources(namespace, deployment, expect_secret=has_hf_token())
        client.request(
            "GET",
            f"/v1/projects/{project_id}/models/{model_name}/logs?tail=20",
            token=token,
        )

        delete = client.request(
            "DELETE",
            f"/v1/projects/{project_id}/models/{model_deployment_id}",
            token=token,
            headers={"Idempotency-Key": f"delete-{suffix}"},
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
        print(f"Real Kubernetes smoke test passed for namespace {namespace}.")
    finally:
        cleanup(client, token, project_id, model_deployment_id, namespace)


def verify_created_resources(
    namespace: str,
    deployment: dict[str, Any],
    *,
    expect_secret: bool,
) -> None:
    """Assert the worker created the expected Kubernetes resources."""
    get("namespace", namespace)
    get("pvc", f"{deployment['name']}-hf-cache", namespace)
    get("deployment", deployment["k8s_deployment_name"], namespace)
    get("service", deployment["k8s_service_name"], namespace)
    get("hpa", deployment["k8s_hpa_name"], namespace)
    if expect_secret:
        get("secret", f"{deployment['name']}-secrets", namespace)


def verify_deleted_resources(
    namespace: str,
    deployment: dict[str, Any],
    *,
    expect_secret: bool,
) -> None:
    """Assert delete_model removed traffic/runtime resources."""
    missing("deployment", deployment["k8s_deployment_name"], namespace)
    missing("service", deployment["k8s_service_name"], namespace)
    missing("hpa", deployment["k8s_hpa_name"], namespace)
    if expect_secret:
        missing("secret", f"{deployment['name']}-secrets", namespace)


def verify_kubectl_available() -> None:
    """Fail early when kubectl cannot reach the current cluster."""
    run_kubectl(["version", "--client=true"])
    run_kubectl(["cluster-info"])


def get(kind: str, name: str, namespace: str | None = None) -> None:
    """Run `kubectl get` and require the resource to exist."""
    args = ["get", kind, name]
    if namespace:
        args.extend(["-n", namespace])
    run_kubectl(args)
    print(f"OK kubectl get {kind}/{name}")


def missing(kind: str, name: str, namespace: str | None = None) -> None:
    """Run `kubectl get` and require the resource to be absent."""
    args = ["get", kind, name]
    if namespace:
        args.extend(["-n", namespace])
    result = run_kubectl(args, check=False)
    if result.returncode == 0:
        raise RuntimeError(f"Expected Kubernetes {kind}/{name} to be deleted.")
    print(f"OK kubectl missing {kind}/{name}")


def run_kubectl(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run kubectl without shell interpolation."""
    result = subprocess.run(
        ["kubectl", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"kubectl {' '.join(args)} failed: {result.stderr or result.stdout}"
        )
    return result


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run one local command without shell interpolation."""
    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr or result.stdout}")
    return result


def has_hf_token() -> bool:
    """Return whether the worker should create a Hugging Face token Secret."""
    return bool(os.getenv("HUGGING_FACE_TOKEN"))


def cleanup(
    client: SmokeClient,
    token: str | None,
    project_id: str | None,
    model_deployment_id: str | None,
    namespace: str | None,
) -> None:
    """Best-effort cleanup for failed real-cluster smoke runs."""
    if token and project_id and model_deployment_id:
        try:
            client.request(
                "DELETE",
                f"/v1/projects/{project_id}/models/{model_deployment_id}",
                token=token,
                headers={"Idempotency-Key": f"cleanup-{uuid.uuid4().hex}"},
                expected_status={202, 404},
            )
        except Exception:
            pass
    if token and project_id:
        try:
            client.request(
                "DELETE",
                f"/v1/projects/{project_id}",
                token=token,
                expected_status={200, 404},
            )
        except Exception:
            pass
    if token:
        try:
            client.request("DELETE", "/v1/users/me", token=token, expected_status={200, 401})
        except Exception:
            pass
    if namespace:
        # Namespace deletion removes retained PVCs from failed smoke attempts.
        run_kubectl(["delete", "namespace", namespace, "--ignore-not-found=true"], check=False)
        time.sleep(1)


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(
        description="Smoke test MiniTen against a real local Kubernetes cluster.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Base URL of the running MiniTen API.",
    )
    parser.add_argument(
        "--model-id",
        default=os.getenv("MINITEN_K8S_TEST_MODEL_ID", DEFAULT_MODEL_ID),
        help="Small Hugging Face model ID to deploy through vLLM.",
    )
    parser.add_argument(
        "--smoke-image",
        default=os.getenv("K8S_SMOKE_TEST_IMAGE", DEFAULT_SMOKE_IMAGE),
        help="Tiny image used for local Kubernetes readiness smoke tests.",
    )
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()
    try:
        run_real_k8s_smoke_test(args.base_url, args.model_id, args.smoke_image)
    except Exception as exc:
        print(f"Real Kubernetes smoke test failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
