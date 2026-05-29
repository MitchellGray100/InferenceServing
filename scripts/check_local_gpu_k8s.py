"""Preflight checks for running the local vLLM GPU smoke test.

The GPU smoke test requests `nvidia.com/gpu` in the Kubernetes pod. A host GPU
alone is not enough: the local Kubernetes node must advertise that extended
resource through the NVIDIA container runtime/device plugin path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


GPU_RESOURCE_NAME = "nvidia.com/gpu"
DEFAULT_MIN_VLLM_COMPUTE_CAPABILITY = "7.5"


def run_kubectl_json(args: list[str]) -> dict:
    """Run kubectl and parse a JSON response."""
    result = subprocess.run(
        ["kubectl", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def run_command(args: list[str]) -> str:
    """Run a local command and return stdout."""
    result = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def current_kubernetes_context() -> str:
    """Return the active kubectl context name."""
    return run_command(["kubectl", "config", "current-context"]).strip()


def docker_operating_system() -> str:
    """Return Docker daemon operating system details."""
    return run_command(["docker", "info", "--format", "{{.OperatingSystem}}"]).strip()


def validate_supported_gpu_kubernetes_backend() -> bool:
    """Reject local kind-on-Docker-Desktop GPU smoke runs.

    Docker Desktop can run GPU containers with `docker run --gpus all`, but kind
    pods are launched by containerd inside the kind node container. That nested
    runtime does not receive Docker Desktop's NVIDIA runtime injection, so vLLM
    pods can be scheduled but cannot initialize CUDA/NVML.
    """
    context = current_kubernetes_context()
    docker_os = docker_operating_system().lower()
    if context.startswith("kind-") and "docker desktop" in docker_os:
        print(
            "The active Kubernetes context is kind on Docker Desktop. This local "
            "backend can schedule a fake GPU resource, but vLLM pods cannot "
            "initialize CUDA/NVML because Docker Desktop's `--gpus all` runtime "
            "injection does not propagate into kind's nested containerd.",
            file=sys.stderr,
        )
        print(
            "Use `make test-local-vllm` for local CPU validation, or run the GPU "
            "smoke test against a Linux/WSL Kubernetes cluster configured with "
            "the NVIDIA container runtime/device plugin.",
            file=sys.stderr,
        )
        return False
    return True


def run_docker_gpu_query() -> str:
    """Return host GPU compute capabilities reported by nvidia-smi in Docker."""
    image = os.getenv("MINITEN_GPU_PROBE_IMAGE", "nvidia/cuda:12.4.1-base-ubuntu22.04")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--gpus",
            "all",
            image,
            "nvidia-smi",
            "--query-gpu=name,compute_cap",
            "--format=csv,noheader",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def parse_compute_capability(value: str) -> tuple[int, int]:
    """Parse a compute capability string such as '7.5' into comparable parts."""
    major, _, minor = value.strip().partition(".")
    return int(major), int(minor or "0")


def docker_gpu_compute_capabilities(query_output: str) -> list[tuple[str, str]]:
    """Parse `nvidia-smi --query-gpu=name,compute_cap` output."""
    gpus = []
    for line in query_output.splitlines():
        if not line.strip():
            continue
        name, _, capability = line.rpartition(",")
        gpus.append((name.strip(), capability.strip()))
    return gpus


def validate_vllm_gpu_compute_capability(gpus: list[tuple[str, str]]) -> bool:
    """Return true when at least one Docker-visible GPU can run current vLLM."""
    minimum = os.getenv(
        "MINITEN_VLLM_GPU_MIN_COMPUTE_CAPABILITY",
        DEFAULT_MIN_VLLM_COMPUTE_CAPABILITY,
    )
    min_capability = parse_compute_capability(minimum)
    compatible = [
        (name, capability)
        for name, capability in gpus
        if parse_compute_capability(capability) >= min_capability
    ]
    if compatible:
        summary = ", ".join(f"{name}=sm_{capability.replace('.', '')}" for name, capability in compatible)
        print(f"OK Docker GPU compute capability for vLLM: {summary}")
        return True

    detected = ", ".join(f"{name}=sm_{capability.replace('.', '')}" for name, capability in gpus)
    print(
        "Docker can see NVIDIA GPUs, but none meet the current vLLM GPU image "
        f"minimum compute capability {minimum}.",
        file=sys.stderr,
    )
    print(f"Detected GPUs: {detected or 'none'}", file=sys.stderr)
    print(
        "A GTX 1080 Ti is Pascal compute capability 6.1, which is below the "
        "prebuilt vLLM GPU image requirement. Use the CPU vLLM smoke test, a "
        "newer GPU, or a custom vLLM build/image compiled for Pascal.",
        file=sys.stderr,
    )
    return False


def allocatable_gpu_count(node: dict) -> int:
    """Return the allocatable NVIDIA GPU count for one Kubernetes node."""
    value = node.get("status", {}).get("allocatable", {}).get(GPU_RESOURCE_NAME, "0")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def gpu_nodes(nodes_payload: dict) -> list[tuple[str, int]]:
    """Return nodes with allocatable NVIDIA GPUs."""
    nodes = []
    for node in nodes_payload.get("items", []):
        count = allocatable_gpu_count(node)
        if count > 0:
            nodes.append((node.get("metadata", {}).get("name", "unknown"), count))
    return nodes


def main() -> int:
    """Fail early when local Kubernetes cannot schedule GPU pods."""
    try:
        if not validate_supported_gpu_kubernetes_backend():
            return 1
        if not validate_vllm_gpu_compute_capability(
            docker_gpu_compute_capabilities(run_docker_gpu_query())
        ):
            return 1
    except FileNotFoundError:
        print("docker is not installed or not on PATH.", file=sys.stderr)
        return 1
    except (subprocess.CalledProcessError, ValueError) as exc:
        print("Docker could not report NVIDIA GPU compute capability.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1

    try:
        nodes_payload = run_kubectl_json(["get", "nodes", "-o", "json"])
    except FileNotFoundError:
        print("kubectl is not installed or not on PATH.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print("kubectl could not read local Kubernetes nodes.", file=sys.stderr)
        print(exc.stderr or exc.stdout, file=sys.stderr)
        return 1

    nodes = gpu_nodes(nodes_payload)
    if not nodes:
        print(
            "Local Kubernetes does not advertise allocatable nvidia.com/gpu.",
            file=sys.stderr,
        )
        print(
            "The GPU smoke test needs a kind/minikube node with NVIDIA GPU "
            "runtime support and the NVIDIA device plugin installed.",
            file=sys.stderr,
        )
        print(
            "After configuring GPU support, verify with: "
            "kubectl get nodes -o jsonpath=\"{.items[*].status.allocatable.nvidia\\.com/gpu}\"",
            file=sys.stderr,
        )
        return 1

    summary = ", ".join(f"{name}={count}" for name, count in nodes)
    print(f"OK Kubernetes GPU allocatable {GPU_RESOURCE_NAME}: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
