"""Install or verify metrics-server for MiniTen's local Kubernetes cluster."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from typing import Any


DEFAULT_CLUSTER_NAME = "miniten"
METRICS_SERVER_NAMESPACE = "kube-system"
METRICS_SERVER_DEPLOYMENT = "metrics-server"
METRICS_SERVER_URL = (
    "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/"
    "components.yaml"
)
KIND_INSECURE_TLS_ARG = "--kubelet-insecure-tls"


def ensure_metrics_server(cluster_name: str) -> None:
    """Install metrics-server once and make it usable with kind."""
    require_tool("kubectl")
    context = f"kind-{cluster_name}"

    if deployment_exists(context):
        print("metrics-server already installed.")
    else:
        print("Installing metrics-server.")
        run(["kubectl", "apply", "--context", context, "-f", METRICS_SERVER_URL])

    deployment = read_deployment(context)
    if has_container_arg(deployment, KIND_INSECURE_TLS_ARG):
        print("metrics-server already has kind kubelet TLS patch.")
    else:
        print("Patching metrics-server for kind kubelet TLS.")
        run(
            [
                "kubectl",
                "patch",
                "deployment",
                METRICS_SERVER_DEPLOYMENT,
                "-n",
                METRICS_SERVER_NAMESPACE,
                "--context",
                context,
                "--type=json",
                "-p",
                json.dumps(
                    [
                        {
                            "op": "add",
                            "path": "/spec/template/spec/containers/0/args/-",
                            "value": KIND_INSECURE_TLS_ARG,
                        }
                    ]
                ),
            ]
        )

    run(
        [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{METRICS_SERVER_DEPLOYMENT}",
            "-n",
            METRICS_SERVER_NAMESPACE,
            "--context",
            context,
            "--timeout=120s",
        ]
    )


def deployment_exists(context: str) -> bool:
    """Return whether metrics-server already exists."""
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "deployment",
            METRICS_SERVER_DEPLOYMENT,
            "-n",
            METRICS_SERVER_NAMESPACE,
            "--context",
            context,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def read_deployment(context: str) -> dict[str, Any]:
    """Read the metrics-server Deployment as JSON, allowing API propagation lag."""
    last_error: RuntimeError | None = None
    for attempt in range(1, 13):
        try:
            result = run(
                [
                    "kubectl",
                    "get",
                    "deployment",
                    METRICS_SERVER_DEPLOYMENT,
                    "-n",
                    METRICS_SERVER_NAMESPACE,
                    "--context",
                    context,
                    "-o",
                    "json",
                ],
                capture=True,
            )
            return json.loads(result.stdout)
        except (RuntimeError, json.JSONDecodeError) as exc:
            last_error = RuntimeError(str(exc))
            if attempt < 12:
                time.sleep(2)
    assert last_error is not None
    raise last_error


def has_container_arg(deployment: dict[str, Any], arg: str) -> bool:
    """Return whether the first metrics-server container already has an arg."""
    containers = (
        deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    if not containers:
        return False
    return arg in containers[0].get("args", [])


def require_tool(name: str) -> None:
    """Fail with a direct install hint when kubectl is missing."""
    if shutil.which(name) is None:
        raise RuntimeError(
            "Required tool not found on PATH: kubectl. Install kubectl before "
            "setting up MiniTen's local Kubernetes environment."
        )


def run(
    args: list[str], *, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run one external command without shell interpolation."""
    result = subprocess.run(
        args,
        check=False,
        capture_output=capture,
        text=True,
    )
    if result.returncode != 0:
        output = result.stderr if capture else ""
        raise RuntimeError(f"{' '.join(args)} failed. {output}".strip())
    return result


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Install metrics-server for MiniTen's local kind cluster."
    )
    parser.add_argument(
        "--cluster-name",
        default=os.getenv("MINITEN_KIND_CLUSTER_NAME", DEFAULT_CLUSTER_NAME),
        help="kind cluster name.",
    )
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()
    try:
        ensure_metrics_server(args.cluster_name)
    except RuntimeError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
