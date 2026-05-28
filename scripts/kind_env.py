"""Manage the local kind cluster used by MiniTen development commands."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_KUBE_DIR = PROJECT_ROOT / ".local" / "kube"
LOCAL_KUBECONFIG = LOCAL_KUBE_DIR / "config"
DEFAULT_CLUSTER_NAME = "miniten"


def ensure_kind_environment(cluster_name: str) -> None:
    """Create or reuse a kind cluster and export a Docker-friendly kubeconfig."""
    require_tool("docker")
    require_tool("kind")
    require_tool("kubectl")
    run(["docker", "info"])

    if cluster_name in existing_clusters():
        print(f"kind cluster already exists: {cluster_name}")
    else:
        print(f"Creating kind cluster: {cluster_name}")
        run(["kind", "create", "cluster", "--name", cluster_name])

    export_docker_kubeconfig(cluster_name)
    run(["kubectl", "cluster-info", "--context", f"kind-{cluster_name}"])
    run(["kubectl", "get", "nodes", "--context", f"kind-{cluster_name}"])


def delete_kind_environment(cluster_name: str) -> None:
    """Delete the local kind cluster and generated Docker kubeconfig."""
    if shutil.which("kind") is None:
        print("kind is not installed; skipping kind cluster cleanup.")
    elif cluster_name in existing_clusters():
        print(f"Deleting kind cluster: {cluster_name}")
        run(["kind", "delete", "cluster", "--name", cluster_name])
    else:
        print(f"kind cluster does not exist: {cluster_name}")

    if LOCAL_KUBECONFIG.exists():
        LOCAL_KUBECONFIG.unlink()
        print(f"Removed generated kubeconfig: {LOCAL_KUBECONFIG}")


def export_docker_kubeconfig(cluster_name: str) -> None:
    """Write kind kubeconfig for Compose workers.

    The Compose worker runs with host networking on Docker Desktop's Linux VM,
    so it can use kind's original localhost API server endpoint. Keeping the
    original server host preserves TLS hostname verification against kind's
    generated certificate.
    """
    result = run(["kind", "get", "kubeconfig", "--name", cluster_name], capture=True)

    LOCAL_KUBE_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_KUBECONFIG.write_text(result.stdout, encoding="utf-8")
    print(f"Wrote Docker worker kubeconfig: {LOCAL_KUBECONFIG}")


def existing_clusters() -> set[str]:
    """Return the set of kind cluster names on this machine."""
    result = run(["kind", "get", "clusters"], capture=True)
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def require_tool(name: str) -> None:
    """Fail with an install hint when a required local tool is missing."""
    if shutil.which(name) is not None:
        return

    if name == "kind":
        hint = "Install with: winget install --id Kubernetes.kind --exact"
    elif name == "kubectl":
        hint = "Install with: winget install --id Kubernetes.kubectl --exact"
    else:
        hint = "Install Docker Desktop and make sure Docker is running."

    raise RuntimeError(f"Required tool not found on PATH: {name}. {hint}")


def run(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
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
    parser = argparse.ArgumentParser(description="Manage MiniTen's local kind cluster.")
    parser.add_argument("command", choices=["ensure", "delete"])
    parser.add_argument(
        "--cluster-name",
        default=os.getenv("MINITEN_KIND_CLUSTER_NAME", DEFAULT_CLUSTER_NAME),
        help="kind cluster name to create/delete.",
    )
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()
    try:
        if args.command == "ensure":
            ensure_kind_environment(args.cluster_name)
        else:
            delete_kind_environment(args.cluster_name)
    except RuntimeError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
