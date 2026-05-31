"""Kubernetes deployment lifecycle operations.

The deployment worker should call this module instead of reaching directly into
Kubernetes API clients. Keeping the operation order here makes retries
predictable and gives tests a small surface to verify.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.config import Config
from app.k8s import client as k8s_client
from app.k8s.manifests import build_model_manifests, model_selector_labels
from app.k8s.names import build_model_resource_names


logger = logging.getLogger(__name__)
POD_FAILURE_REASONS = {
    "CrashLoopBackOff",
    "CreateContainerConfigError",
    "CreateContainerError",
    "ErrImagePull",
    "ImagePullBackOff",
    "InvalidImageName",
    "RunContainerError",
}
POD_TERMINAL_FAILURE_REASONS = {
    "Error",
    "OOMKilled",
}


def apply_model_deployment(
    clients: k8s_client.KubernetesClients,
    deployment: dict[str, Any],
    *,
    pvc_size: str = Config.DEFAULT_PVC_SIZE,
    hugging_face_token: str | None = None,
) -> None:
    """Apply Namespace, cache PVC, optional Secret, Deployment, Service, and HPA."""
    # Build every manifest from the same deployment record so Kubernetes state
    # matches the desired state persisted by the API request.
    manifests = build_model_manifests(
        deployment,
        pvc_size=pvc_size,
        hugging_face_token=hugging_face_token,
    )
    logger.info(
        "Applying Kubernetes model deployment model_deployment_id=%s namespace=%s deployment=%s.",
        deployment.get("model_deployment_id"),
        deployment["k8s_namespace"],
        deployment["k8s_deployment_name"],
    )

    # Namespace and PVC must exist before pods can be scheduled with the cache
    # volume mounted.
    k8s_client.apply_namespace(clients, manifests["namespace"])
    k8s_client.apply_pvc(clients, manifests["pvc"])

    if manifests["secret"] is not None:
        k8s_client.apply_secret(clients, manifests["secret"])

    # Apply Deployment before Service/HPA so selectors and scale targets can
    # resolve immediately after the worker finishes.
    k8s_client.apply_deployment(clients, manifests["deployment"])
    k8s_client.apply_service(clients, manifests["service"])

    if manifests["hpa"] is not None:
        k8s_client.apply_hpa(clients, manifests["hpa"])
        k8s_client.read_hpa(clients, deployment["k8s_namespace"], deployment["k8s_hpa_name"])
    else:
        # A settings update can turn autoscaling off after an HPA already
        # exists. Reconcile absence explicitly so the old HPA cannot continue
        # controlling replicas behind MiniTen's back.
        k8s_client.delete_hpa(
            clients,
            deployment["k8s_namespace"],
            deployment["k8s_hpa_name"],
        )

    wait_for_model_ready(clients, deployment)
    logger.info(
        "Applied Kubernetes model deployment model_deployment_id=%s.",
        deployment.get("model_deployment_id"),
    )


def delete_model_deployment(
    clients: k8s_client.KubernetesClients,
    deployment: dict[str, Any],
    *,
    delete_cache: bool = False,
    delete_secret: bool = True,
) -> None:
    """Delete model Kubernetes resources in dependency-safe order.

    The Service/HPA/Deployment are always removed. The PVC defaults to retained
    so cached model weights survive stop/delete retries until product policy
    says otherwise.
    """
    names = build_model_resource_names(
        deployment["k8s_namespace"],
        deployment["k8s_service_name"],
    )
    namespace = deployment["k8s_namespace"]
    logger.info(
        "Deleting Kubernetes model resources model_deployment_id=%s namespace=%s deployment=%s delete_cache=%s.",
        deployment.get("model_deployment_id"),
        namespace,
        deployment["k8s_deployment_name"],
        delete_cache,
    )

    # Delete traffic/scaling resources before deleting pods. Each delete helper
    # treats 404 as success, which keeps retries safe.
    k8s_client.delete_hpa(clients, namespace, deployment["k8s_hpa_name"])
    k8s_client.delete_service(clients, namespace, deployment["k8s_service_name"])
    k8s_client.delete_deployment(
        clients,
        namespace,
        deployment["k8s_deployment_name"],
    )

    if delete_secret:
        k8s_client.delete_secret(clients, namespace, names["k8s_secret_name"])

    if delete_cache:
        k8s_client.delete_pvc(clients, namespace, names["k8s_pvc_name"])
    logger.info(
        "Deleted Kubernetes model resources model_deployment_id=%s.",
        deployment.get("model_deployment_id"),
    )


def scale_model_deployment(
    clients: k8s_client.KubernetesClients,
    deployment: dict[str, Any],
    replicas: int,
) -> Any:
    """Patch Deployment replicas for non-HPA scale operations."""
    # Kubernetes exposes the scale subresource for lightweight replica changes
    # without resending the full Deployment manifest.
    body = {
        "spec": {
            "replicas": replicas,
        }
    }
    logger.info(
        "Scaling Kubernetes model deployment model_deployment_id=%s namespace=%s deployment=%s replicas=%s.",
        deployment.get("model_deployment_id"),
        deployment["k8s_namespace"],
        deployment["k8s_deployment_name"],
        replicas,
    )
    result = clients.apps.patch_namespaced_deployment_scale(
        deployment["k8s_deployment_name"],
        deployment["k8s_namespace"],
        body,
    )
    if replicas == 0:
        wait_for_model_stopped(clients, deployment)
    else:
        wait_for_model_ready(clients, deployment, expected_replicas=replicas)
    return result


def stop_model_deployment(
    clients: k8s_client.KubernetesClients,
    deployment: dict[str, Any],
) -> Any:
    """Stop a model deployment without letting an HPA scale it back up."""
    if deployment["autoscaling_enabled"]:
        k8s_client.delete_hpa(
            clients,
            deployment["k8s_namespace"],
            deployment["k8s_hpa_name"],
        )

    return scale_model_deployment(clients, deployment, 0)


def wait_for_model_ready(
    clients: k8s_client.KubernetesClients,
    deployment: dict[str, Any],
    *,
    expected_replicas: int | None = None,
    timeout_seconds: float = Config.WORKER_READINESS_TIMEOUT_SECONDS,
    poll_interval_seconds: float = Config.WORKER_READINESS_POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Wait until Kubernetes resources and pods show the deployment is usable."""
    namespace = deployment["k8s_namespace"]
    deployment_name = deployment["k8s_deployment_name"]
    service_name = deployment["k8s_service_name"]
    expected = int(expected_replicas or deployment.get("replicas") or 1)
    deadline = time.monotonic() + timeout_seconds
    last_status: dict[str, Any] = {}

    while time.monotonic() <= deadline:
        status = inspect_model_readiness(clients, deployment, expected_replicas=expected)
        last_status = status
        if status["failed"]:
            raise RuntimeError(status["reason"])
        if status["ready"]:
            logger.info(
                "Kubernetes model deployment ready namespace=%s deployment=%s service=%s ready_pods=%s.",
                namespace,
                deployment_name,
                service_name,
                status["ready_pods"],
            )
            return status
        time.sleep(poll_interval_seconds)

    raise RuntimeError(
        "Timed out waiting for Kubernetes model readiness "
        f"namespace={namespace} deployment={deployment_name} service={service_name} "
        f"last_status={last_status}"
    )


def wait_for_model_stopped(
    clients: k8s_client.KubernetesClients,
    deployment: dict[str, Any],
    *,
    timeout_seconds: float = Config.WORKER_READINESS_TIMEOUT_SECONDS,
    poll_interval_seconds: float = Config.WORKER_READINESS_POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Wait until Kubernetes reports zero available replicas for a stopped model."""
    namespace = deployment["k8s_namespace"]
    deployment_name = deployment["k8s_deployment_name"]
    deadline = time.monotonic() + timeout_seconds
    last_status: dict[str, Any] = {}

    while time.monotonic() <= deadline:
        status = inspect_model_readiness(clients, deployment, expected_replicas=0)
        last_status = status
        if status["failed"]:
            raise RuntimeError(status["reason"])
        if status["available_replicas"] == 0 and status["ready_pods"] == 0:
            logger.info(
                "Kubernetes model deployment stopped namespace=%s deployment=%s.",
                namespace,
                deployment_name,
            )
            return status
        time.sleep(poll_interval_seconds)

    raise RuntimeError(
        "Timed out waiting for Kubernetes model stop "
        f"namespace={namespace} deployment={deployment_name} last_status={last_status}"
    )


def inspect_model_readiness(
    clients: k8s_client.KubernetesClients,
    deployment: dict[str, Any],
    *,
    expected_replicas: int,
) -> dict[str, Any]:
    """Return a structured snapshot of Kubernetes readiness for a deployment."""
    namespace = deployment["k8s_namespace"]
    deployment_name = deployment["k8s_deployment_name"]
    service_name = deployment["k8s_service_name"]
    k8s_deployment = k8s_client.read_deployment(clients, namespace, deployment_name)
    k8s_client.read_service(clients, namespace, service_name)
    pods = k8s_client.list_pods(
        clients,
        namespace,
        label_selector=build_model_label_selector(deployment["name"]),
    )
    failure_reason = first_pod_failure_reason(pods)
    failure_code = classify_kubernetes_failure(failure_reason)
    available_replicas = int(value_at(k8s_deployment, "status.available_replicas") or 0)
    ready_pods = sum(1 for pod in pods if is_pod_ready(pod))
    scheduled_pods = sum(1 for pod in pods if is_pod_scheduled(pod))
    deployment_available = deployment_has_available_condition(k8s_deployment)

    return {
        "ready": (
            expected_replicas == 0
            or (
                deployment_available
                and available_replicas >= expected_replicas
                and scheduled_pods >= expected_replicas
                and ready_pods >= expected_replicas
            )
        ),
        "failed": failure_reason is not None,
        "failure_code": failure_code,
        "reason": failure_reason,
        "deployment_available": deployment_available,
        "available_replicas": available_replicas,
        "scheduled_pods": scheduled_pods,
        "ready_pods": ready_pods,
        "pod_count": len(pods),
        "pods": summarize_pods(pods),
    }


def read_model_logs(
    clients: k8s_client.KubernetesClients,
    deployment: dict[str, Any],
    *,
    tail_lines: int,
) -> list[dict[str, str]]:
    """Read recent logs from pods belonging to one model deployment."""
    namespace = deployment["k8s_namespace"]
    pods = k8s_client.list_pods(
        clients,
        namespace,
        label_selector=build_model_label_selector(deployment["name"]),
    )
    logs = []

    for pod in pods:
        pod_name = value_at(pod, "metadata.name")
        if not pod_name:
            continue
        try:
            text = k8s_client.read_pod_log(
                clients,
                namespace,
                pod_name,
                tail_lines=tail_lines,
            )
        except Exception as exc:
            logger.warning(
                "Failed to read Kubernetes pod logs namespace=%s pod=%s: %s.",
                namespace,
                pod_name,
                exc,
            )
            text = f"Failed to read pod logs: {exc}"
        logs.append(
            {
                "pod": pod_name,
                "text": text,
            }
        )

    return logs


def build_model_label_selector(model_name: str) -> str:
    """Return a Kubernetes label selector for one model deployment."""
    return ",".join(
        f"{key}={value}" for key, value in model_selector_labels(model_name).items()
    )


def deployment_has_available_condition(deployment: Any) -> bool:
    """Return whether the Deployment has an Available=True condition."""
    conditions = value_at(deployment, "status.conditions") or []
    for condition in conditions:
        if value_at(condition, "type") == "Available" and value_at(condition, "status") == "True":
            return True
    return False


def is_pod_scheduled(pod: Any) -> bool:
    """Return whether Kubernetes scheduled the pod onto a node."""
    return pod_has_condition(pod, "PodScheduled", "True")


def is_pod_ready(pod: Any) -> bool:
    """Return whether Kubernetes marks the pod Ready."""
    return pod_has_condition(pod, "Ready", "True")


def pod_has_condition(pod: Any, condition_type: str, status: str) -> bool:
    """Return whether a pod has a condition with the requested status."""
    for condition in value_at(pod, "status.conditions") or []:
        if (
            value_at(condition, "type") == condition_type
            and value_at(condition, "status") == status
        ):
            return True
    return False


def first_pod_failure_reason(pods: list[Any]) -> str | None:
    """Return a useful failure reason from pod container waiting states."""
    for pod in pods:
        pod_name = value_at(pod, "metadata.name") or "unknown"
        for status in value_at(pod, "status.container_statuses") or []:
            waiting = value_at(status, "state.waiting")
            reason = value_at(waiting, "reason") if waiting is not None else None
            message = value_at(waiting, "message") if waiting is not None else None
            if reason in POD_FAILURE_REASONS:
                detail = f": {message}" if message else ""
                return f"Pod {pod_name} is waiting with {reason}{detail}"
            if value_at(status, "ready") is True or value_at(status, "state.running"):
                continue
            terminated = value_at(status, "last_state.terminated")
            terminated_reason = (
                value_at(terminated, "reason") if terminated is not None else None
            )
            terminated_message = (
                value_at(terminated, "message") if terminated is not None else None
            )
            if terminated_reason in POD_TERMINAL_FAILURE_REASONS:
                detail = f": {terminated_message}" if terminated_message else ""
                return f"Pod {pod_name} previously terminated with {terminated_reason}{detail}"
    return None


def classify_kubernetes_failure(reason: str | None) -> str | None:
    """Map Kubernetes failure text to stable API/worker categories."""
    if reason is None:
        return None
    lowered = reason.lower()
    if "imagepull" in lowered or "errimagepull" in lowered or "invalidimagename" in lowered:
        return "image_pull"
    if "oomkilled" in lowered or "insufficient memory" in lowered:
        return "insufficient_memory"
    if "insufficient" in lowered and "cpu" in lowered:
        return "insufficient_cpu"
    if "gpu" in lowered or "nvidia" in lowered or "cuda" in lowered:
        return "gpu_unavailable"
    if "hf_token" in lowered or "hugging face" in lowered or "401" in lowered or "403" in lowered:
        return "model_download_auth"
    return "pod_failure"


def summarize_pods(pods: list[Any]) -> list[dict[str, Any]]:
    """Return compact pod diagnostics for readiness timeout/error messages."""
    summaries = []
    for pod in pods:
        container_states = []
        for status in value_at(pod, "status.container_statuses") or []:
            container_states.append(
                {
                    "name": value_at(status, "name"),
                    "ready": value_at(status, "ready"),
                    "restart_count": value_at(status, "restart_count"),
                    "waiting_reason": value_at(status, "state.waiting.reason"),
                    "waiting_message": value_at(status, "state.waiting.message"),
                    "terminated_reason": value_at(status, "state.terminated.reason"),
                }
            )
        summaries.append(
            {
                "name": value_at(pod, "metadata.name"),
                "phase": value_at(pod, "status.phase"),
                "pod_ip": value_at(pod, "status.pod_ip"),
                "scheduled": is_pod_scheduled(pod),
                "ready": is_pod_ready(pod),
                "containers": container_states,
            }
        )
    return summaries


def value_at(obj: Any, path: str) -> Any:
    """Read dotted attributes/keys from Kubernetes objects or plain dicts."""
    value = obj
    for part in path.split("."):
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = getattr(value, part, None)
    return value
