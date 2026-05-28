"""Kubernetes deployment lifecycle operations.

The deployment worker should call this module instead of reaching directly into
Kubernetes API clients. Keeping the operation order here makes retries
predictable and gives tests a small surface to verify.
"""

from __future__ import annotations

from typing import Any

from app.config import Config
from app.k8s import client as k8s_client
from app.k8s.manifests import build_model_manifests
from app.k8s.names import build_model_resource_names


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
    names = build_model_resource_names(deployment["k8s_namespace"], deployment["name"])
    namespace = names["k8s_namespace"]

    # Delete traffic/scaling resources before deleting pods. Each delete helper
    # treats 404 as success, which keeps retries idempotent.
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
    return clients.apps.patch_namespaced_deployment_scale(
        deployment["k8s_deployment_name"],
        deployment["k8s_namespace"],
        body,
    )
