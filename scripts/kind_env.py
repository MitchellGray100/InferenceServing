"""Manage the local kind cluster used by MiniTen development commands."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_KUBE_DIR = PROJECT_ROOT / ".local" / "kube"
LOCAL_KUBECONFIG = LOCAL_KUBE_DIR / "config"
DEFAULT_CLUSTER_NAME = "miniten"
DEFAULT_GPU_PROBE_IMAGE = "nvidia/cuda:12.4.1-base-ubuntu22.04"
NVIDIA_DEVICE_PLUGIN_URL = (
    "https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.1/"
    "deployments/static/nvidia-device-plugin.yml"
)
NVIDIA_DRIVER_LIBRARY_NAMES = ("libcuda.so.1", "libnvidia-ml.so.1")
NVIDIA_NODE_DRIVER_LIBRARY_DIR = "/usr/local/nvidia/lib64"
NVIDIA_GPU_RESOURCE = "nvidia.com/gpu"
KIND_VALIDATION_ATTEMPTS = 12
KIND_VALIDATION_DELAY_SECONDS = 2.0


def ensure_kind_environment(cluster_name: str, *, gpu: bool = False) -> None:
    """Create or reuse a kind cluster and export a Docker-friendly kubeconfig."""
    require_tool("docker")
    require_tool("kind")
    require_tool("kubectl")
    run(["docker", "info"])
    if gpu:
        verify_docker_gpu_runtime()

    cluster_exists = cluster_name in existing_clusters()
    if cluster_exists:
        print(f"kind cluster already exists: {cluster_name}")
        start_kind_environment(cluster_name)
    else:
        print(f"Creating kind cluster: {cluster_name}")
        run(["kind", "create", "cluster", "--name", cluster_name])

    try:
        export_docker_kubeconfig(cluster_name)
    except RuntimeError as exc:
        if not cluster_exists:
            raise
        print(
            "Existing kind cluster is unusable; recreating it. "
            f"Original error: {exc}"
        )
        run(["kind", "delete", "cluster", "--name", cluster_name])
        run(["kind", "create", "cluster", "--name", cluster_name])
        export_docker_kubeconfig(cluster_name)
    if gpu:
        ensure_kind_gpu_support(cluster_name)
    validate_kind_environment(cluster_name)


def validate_kind_environment(cluster_name: str) -> None:
    """Wait until the restarted kind API and default RBAC are usable."""
    context = f"kind-{cluster_name}"
    run_with_retries(
        ["kubectl", "get", "nodes", "--context", context],
        attempts=KIND_VALIDATION_ATTEMPTS,
        delay_seconds=KIND_VALIDATION_DELAY_SECONDS,
    )
    run_with_retries(
        [
            "kubectl",
            "auth",
            "can-i",
            "list",
            "services",
            "-n",
            "kube-system",
            "--context",
            context,
        ],
        attempts=KIND_VALIDATION_ATTEMPTS,
        delay_seconds=KIND_VALIDATION_DELAY_SECONDS,
        accepted_stdout={"yes"},
    )


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


def start_kind_environment(cluster_name: str) -> None:
    """Start an existing local kind node without deleting cached cluster data."""
    require_tool("docker")
    node_name = kind_control_plane_container_name(cluster_name)
    if not docker_container_exists(node_name):
        print(f"kind control-plane container does not exist: {node_name}")
        return
    if docker_container_running(node_name):
        print(f"kind control-plane container already running: {node_name}")
        return
    print(f"Starting kind control-plane container: {node_name}")
    run(["docker", "start", node_name])


def stop_kind_environment(cluster_name: str) -> None:
    """Stop an existing local kind node while preserving PVC/cache data."""
    if shutil.which("docker") is None:
        print("Docker CLI is not installed; skipping kind stop.")
        return

    node_name = kind_control_plane_container_name(cluster_name)
    if not docker_container_exists(node_name):
        print(f"kind control-plane container does not exist: {node_name}")
        return
    if not docker_container_running(node_name):
        print(f"kind control-plane container already stopped: {node_name}")
        return
    print(f"Stopping kind control-plane container: {node_name}")
    run(["docker", "stop", node_name])


def kind_control_plane_container_name(cluster_name: str) -> str:
    """Return the Docker container name for a single-node kind cluster."""
    return f"{cluster_name}-control-plane"


def docker_container_exists(container_name: str) -> bool:
    """Return whether Docker knows about a container by name."""
    result = subprocess.run(
        ["docker", "inspect", container_name],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def docker_container_running(container_name: str) -> bool:
    """Return whether a Docker container is currently running."""
    result = run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}}",
            container_name,
        ],
        capture=True,
    )
    return result.stdout.strip().lower() == "true"


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


def verify_docker_gpu_runtime() -> None:
    """Fail early unless Docker can run a CUDA container with GPU access."""
    image = os.getenv("MINITEN_GPU_PROBE_IMAGE", DEFAULT_GPU_PROBE_IMAGE)
    run(["docker", "run", "--rm", "--gpus", "all", image, "nvidia-smi"])


def ensure_kind_gpu_support(cluster_name: str) -> None:
    """Make the local kind node usable for NVIDIA GPU smoke tests.

    Docker Desktop injects NVIDIA driver libraries into containers started with
    `--gpus all`. kind node containers are not started that way, so the node can
    have `/dev/dxg` while still missing NVML/CUDA libraries. The device plugin
    needs those libraries to advertise `nvidia.com/gpu`, and vLLM pods need the
    CUDA driver library at runtime.
    """
    node_name = f"{cluster_name}-control-plane"
    copy_nvidia_driver_libraries_to_kind_node(node_name)
    enable_node_nvidia_library_path(node_name)
    install_nvidia_device_plugin(cluster_name)
    patch_node_gpu_capacity(cluster_name, node_name)
    wait_for_allocatable_gpus(cluster_name)


def copy_nvidia_driver_libraries_to_kind_node(node_name: str) -> None:
    """Copy CUDA/NVML driver libraries from a GPU-enabled container into kind."""
    image = os.getenv("MINITEN_GPU_PROBE_IMAGE", DEFAULT_GPU_PROBE_IMAGE)
    with tempfile.TemporaryDirectory(prefix="miniten-gpu-libs-") as temp_dir:
        temp_path = Path(temp_dir)
        container_name = f"miniten-gpu-libs-{os.getpid()}"
        try:
            run(
                [
                    "docker",
                    "create",
                    "--name",
                    container_name,
                    "--gpus",
                    "all",
                    image,
                    "sleep",
                    "300",
                ]
            )
            run(["docker", "start", container_name])
            run(["docker", "exec", container_name, "mkdir", "-p", "/tmp/miniten-gpu-libs"])
            for library_name in NVIDIA_DRIVER_LIBRARY_NAMES:
                source = find_library_in_container(container_name, library_name)
                run(
                    [
                        "docker",
                        "exec",
                        container_name,
                        "cp",
                        "-L",
                        source,
                        f"/tmp/miniten-gpu-libs/{library_name}",
                    ]
                )
                run(
                    [
                        "docker",
                        "cp",
                        f"{container_name}:/tmp/miniten-gpu-libs/{library_name}",
                        str(temp_path),
                    ]
                )
        finally:
            run(["docker", "rm", "-f", container_name])

        run(
            [
                "docker",
                "exec",
                node_name,
                "mkdir",
                "-p",
                NVIDIA_NODE_DRIVER_LIBRARY_DIR,
            ]
        )
        for library_name in NVIDIA_DRIVER_LIBRARY_NAMES:
            run(
                [
                    "docker",
                    "cp",
                    str(temp_path / library_name),
                    f"{node_name}:{NVIDIA_NODE_DRIVER_LIBRARY_DIR}/{library_name}",
                ]
            )
    print(f"Copied NVIDIA driver libraries into kind node: {node_name}")


def find_library_in_container(container_name: str, library_name: str) -> str:
    """Find one NVIDIA driver library inside the GPU probe container."""
    result = run(
        [
            "docker",
            "exec",
            container_name,
            "sh",
            "-lc",
            f"find /usr/lib /usr/local/cuda* -name {library_name} 2>/dev/null | head -n 1",
        ],
        capture=True,
    )
    library_path = result.stdout.strip()
    if not library_path:
        raise RuntimeError(f"Could not find {library_name} in GPU probe container.")
    return library_path


def enable_node_nvidia_library_path(node_name: str) -> None:
    """Add copied NVIDIA libraries to the kind node linker cache."""
    run(
        [
            "docker",
            "exec",
            node_name,
            "sh",
            "-lc",
            (
                f"echo {NVIDIA_NODE_DRIVER_LIBRARY_DIR} "
                "> /etc/ld.so.conf.d/miniten-nvidia.conf && ldconfig"
            ),
        ]
    )


def install_nvidia_device_plugin(cluster_name: str) -> None:
    """Install the NVIDIA device plugin into the local kind cluster."""
    run(
        [
            "kubectl",
            "apply",
            "--context",
            f"kind-{cluster_name}",
            "-f",
            NVIDIA_DEVICE_PLUGIN_URL,
        ]
    )


def patch_node_gpu_capacity(cluster_name: str, node_name: str) -> None:
    """Advertise one local WSL2 GPU when the NVIDIA plugin cannot discover it.

    Docker Desktop's WSL2 GPU path exposes `/dev/dxg`, but the upstream NVIDIA
    device plugin expects the standard Linux NVIDIA device/runtime integration.
    For the single-node local smoke test, we patch the node status so Kubernetes
    can schedule a pod that requests `nvidia.com/gpu`; the pod itself receives
    `/dev/dxg` and copied driver libraries through explicit hostPath mounts.
    """
    patch = {
        "status": {
            "capacity": {NVIDIA_GPU_RESOURCE: "1"},
            "allocatable": {NVIDIA_GPU_RESOURCE: "1"},
        }
    }
    run(
        [
            "kubectl",
            "patch",
            "node",
            node_name,
            "--context",
            f"kind-{cluster_name}",
            "--subresource=status",
            "--type=merge",
            "-p",
            json.dumps(patch),
        ]
    )


def wait_for_allocatable_gpus(cluster_name: str) -> None:
    """Wait for the device plugin to advertise GPUs to Kubernetes."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = run(
            [
                "kubectl",
                "get",
                "nodes",
                "--context",
                f"kind-{cluster_name}",
                "-o",
                (
                    "jsonpath={.items[*].status.allocatable."
                    "nvidia\\.com/gpu}"
                ),
            ],
            capture=True,
        )
        if any(value.isdigit() and int(value) > 0 for value in result.stdout.split()):
            print(f"Kubernetes advertises allocatable {NVIDIA_GPU_RESOURCE}: {result.stdout}")
            return
        time.sleep(2)
    raise RuntimeError(
        f"Kubernetes did not advertise allocatable {NVIDIA_GPU_RESOURCE}. "
        "Check the nvidia-device-plugin DaemonSet logs in kube-system."
    )


def existing_clusters() -> set[str]:
    """Return the set of kind cluster names on this machine."""
    result = run(["kind", "get", "clusters"], capture=True)
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def require_tool(name: str) -> None:
    """Fail with an install hint when a required local tool is missing."""
    if shutil.which(name) is not None:
        return

    if name == "kind":
        hint = (
            "Install kind with Homebrew (`brew install kind`), Chocolatey "
            "(`choco install kind -y`), or the Linux binary install steps in README.md."
        )
    elif name == "kubectl":
        hint = (
            "Install kubectl with Homebrew (`brew install kubectl`), Chocolatey "
            "(`choco install kubernetes-cli -y`), or the Debian/Ubuntu steps in README.md."
        )
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


def run_with_retries(
    args: list[str],
    *,
    attempts: int,
    delay_seconds: float,
    accepted_stdout: set[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command until it succeeds, for kind restart readiness checks."""
    last_error: RuntimeError | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = run(args, capture=accepted_stdout is not None)
            if accepted_stdout is None:
                return result
            if result.stdout.strip().lower() in accepted_stdout:
                return result
            last_error = RuntimeError(
                f"{' '.join(args)} returned unexpected output: {result.stdout.strip()}"
            )
        except RuntimeError as exc:
            last_error = exc

        if attempt < attempts:
            time.sleep(delay_seconds)

    assert last_error is not None
    raise last_error


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Manage MiniTen's local kind cluster.")
    parser.add_argument("command", choices=["ensure", "start", "stop", "delete"])
    parser.add_argument(
        "--cluster-name",
        default=os.getenv("MINITEN_KIND_CLUSTER_NAME", DEFAULT_CLUSTER_NAME),
        help="kind cluster name to create/delete.",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="configure the kind cluster for local NVIDIA GPU smoke tests.",
    )
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()
    try:
        if args.command == "ensure":
            ensure_kind_environment(args.cluster_name, gpu=args.gpu)
        elif args.command == "start":
            start_kind_environment(args.cluster_name)
        elif args.command == "stop":
            stop_kind_environment(args.cluster_name)
        else:
            delete_kind_environment(args.cluster_name)
    except RuntimeError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
