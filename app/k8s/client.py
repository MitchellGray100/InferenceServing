"""Kubernetes client construction and thin apply/delete helpers.

This module keeps Kubernetes imports lazy so unit tests and local tooling can
import MiniTen modules without requiring a configured cluster. The deployment
worker will construct `KubernetesClients` once, then apply manifests generated
by `app.k8s.manifests`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any


KUBERNETES_NOT_FOUND = 404
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KubernetesClients:
    """Typed bundle of Kubernetes API clients used by the worker."""

    core: Any
    apps: Any
    autoscaling: Any


def load_kubernetes_config(*, prefer_in_cluster: bool = True) -> None:
    """Load Kubernetes config for in-cluster or local development execution."""
    from kubernetes import config

    if prefer_in_cluster:
        try:
            # Production workers run inside Kubernetes, so service account
            # credentials should be tried before the developer kubeconfig.
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration.")
            return
        except config.ConfigException:
            # Local development falls back to ~/.kube/config below.
            logger.debug("In-cluster Kubernetes configuration unavailable.")
            pass

    config.load_kube_config()
    logger.info("Loaded local kubeconfig Kubernetes configuration.")


def create_clients(*, load_config: bool = True) -> KubernetesClients:
    """Create Kubernetes API clients for core, apps, and autoscaling resources."""
    if load_config:
        load_kubernetes_config()

    # Import lazily so unit tests can import this module without installing or
    # configuring the Kubernetes package.
    from kubernetes import client

    logger.info("Creating Kubernetes API clients.")
    return KubernetesClients(
        core=client.CoreV1Api(),
        apps=client.AppsV1Api(),
        autoscaling=client.AutoscalingV2Api(),
    )


def apply_namespace(clients: KubernetesClients, manifest: dict[str, Any]) -> Any:
    """Create or patch a Namespace manifest."""
    name = manifest["metadata"]["name"]
    logger.debug("Applying Kubernetes Namespace name=%s.", name)
    return _create_or_patch(
        create=lambda: clients.core.create_namespace(manifest),
        patch=lambda: clients.core.patch_namespace(name, manifest),
    )


def apply_pvc(clients: KubernetesClients, manifest: dict[str, Any]) -> Any:
    """Create or patch a PersistentVolumeClaim manifest."""
    name = manifest["metadata"]["name"]
    namespace = manifest["metadata"]["namespace"]
    logger.debug("Applying Kubernetes PVC namespace=%s name=%s.", namespace, name)
    return _create_or_patch(
        create=lambda: clients.core.create_namespaced_persistent_volume_claim(
            namespace,
            manifest,
        ),
        patch=lambda: clients.core.patch_namespaced_persistent_volume_claim(
            name,
            namespace,
            manifest,
        ),
    )


def apply_secret(clients: KubernetesClients, manifest: dict[str, Any]) -> Any:
    """Create or patch a Secret manifest."""
    name = manifest["metadata"]["name"]
    namespace = manifest["metadata"]["namespace"]
    logger.debug("Applying Kubernetes Secret namespace=%s name=%s.", namespace, name)
    return _create_or_patch(
        create=lambda: clients.core.create_namespaced_secret(namespace, manifest),
        patch=lambda: clients.core.patch_namespaced_secret(name, namespace, manifest),
    )


def apply_deployment(clients: KubernetesClients, manifest: dict[str, Any]) -> Any:
    """Create or patch a Deployment manifest."""
    name = manifest["metadata"]["name"]
    namespace = manifest["metadata"]["namespace"]
    logger.debug("Applying Kubernetes Deployment namespace=%s name=%s.", namespace, name)
    return _create_or_patch(
        create=lambda: clients.apps.create_namespaced_deployment(namespace, manifest),
        patch=lambda: clients.apps.patch_namespaced_deployment(name, namespace, manifest),
    )


def apply_service(clients: KubernetesClients, manifest: dict[str, Any]) -> Any:
    """Create or patch a Service manifest."""
    name = manifest["metadata"]["name"]
    namespace = manifest["metadata"]["namespace"]
    logger.debug("Applying Kubernetes Service namespace=%s name=%s.", namespace, name)
    return _create_or_patch(
        create=lambda: clients.core.create_namespaced_service(namespace, manifest),
        patch=lambda: clients.core.patch_namespaced_service(name, namespace, manifest),
    )


def apply_hpa(clients: KubernetesClients, manifest: dict[str, Any]) -> Any:
    """Create or patch an autoscaling/v2 HorizontalPodAutoscaler manifest."""
    name = manifest["metadata"]["name"]
    namespace = manifest["metadata"]["namespace"]
    logger.debug("Applying Kubernetes HPA namespace=%s name=%s.", namespace, name)
    return _create_or_patch(
        create=lambda: clients.autoscaling.create_namespaced_horizontal_pod_autoscaler(
            namespace,
            manifest,
        ),
        patch=lambda: clients.autoscaling.patch_namespaced_horizontal_pod_autoscaler(
            name,
            namespace,
            manifest,
        ),
    )


def read_deployment(clients: KubernetesClients, namespace: str, name: str) -> Any:
    """Read a Deployment by namespace/name."""
    logger.debug("Reading Kubernetes Deployment namespace=%s name=%s.", namespace, name)
    return clients.apps.read_namespaced_deployment(name, namespace)


def read_service(clients: KubernetesClients, namespace: str, name: str) -> Any:
    """Read a Service by namespace/name."""
    logger.debug("Reading Kubernetes Service namespace=%s name=%s.", namespace, name)
    return clients.core.read_namespaced_service(name, namespace)


def read_hpa(clients: KubernetesClients, namespace: str, name: str) -> Any:
    """Read an HPA by namespace/name."""
    logger.debug("Reading Kubernetes HPA namespace=%s name=%s.", namespace, name)
    return clients.autoscaling.read_namespaced_horizontal_pod_autoscaler(
        name,
        namespace,
    )


def read_pvc(clients: KubernetesClients, namespace: str, name: str) -> Any:
    """Read a PersistentVolumeClaim by namespace/name."""
    logger.debug("Reading Kubernetes PVC namespace=%s name=%s.", namespace, name)
    return clients.core.read_namespaced_persistent_volume_claim(name, namespace)


def read_secret(clients: KubernetesClients, namespace: str, name: str) -> Any:
    """Read a Secret by namespace/name."""
    logger.debug("Reading Kubernetes Secret namespace=%s name=%s.", namespace, name)
    return clients.core.read_namespaced_secret(name, namespace)


def list_pods(
    clients: KubernetesClients,
    namespace: str,
    *,
    label_selector: str,
) -> list[Any]:
    """List pods in a namespace matching a Kubernetes label selector."""
    logger.debug(
        "Listing Kubernetes Pods namespace=%s label_selector=%s.",
        namespace,
        label_selector,
    )
    response = clients.core.list_namespaced_pod(
        namespace,
        label_selector=label_selector,
    )
    return list(getattr(response, "items", []) or [])


def read_pod_log(
    clients: KubernetesClients,
    namespace: str,
    pod_name: str,
    *,
    tail_lines: int,
    timeout_seconds: int = 5,
) -> str:
    """Read recent logs for one pod."""
    logger.debug(
        "Reading Kubernetes Pod logs namespace=%s pod=%s tail_lines=%s.",
        namespace,
        pod_name,
        tail_lines,
    )
    return clients.core.read_namespaced_pod_log(
        pod_name,
        namespace,
        tail_lines=tail_lines,
        _request_timeout=timeout_seconds,
    )


def delete_deployment(clients: KubernetesClients, namespace: str, name: str) -> Any:
    """Delete a Deployment and ignore already-deleted resources."""
    logger.debug("Deleting Kubernetes Deployment namespace=%s name=%s.", namespace, name)
    return _delete_or_ignore_not_found(
        lambda: clients.apps.delete_namespaced_deployment(name, namespace)
    )


def delete_service(clients: KubernetesClients, namespace: str, name: str) -> Any:
    """Delete a Service and ignore already-deleted resources."""
    logger.debug("Deleting Kubernetes Service namespace=%s name=%s.", namespace, name)
    return _delete_or_ignore_not_found(
        lambda: clients.core.delete_namespaced_service(name, namespace)
    )


def delete_hpa(clients: KubernetesClients, namespace: str, name: str) -> Any:
    """Delete an HPA and ignore already-deleted resources."""
    logger.debug("Deleting Kubernetes HPA namespace=%s name=%s.", namespace, name)
    return _delete_or_ignore_not_found(
        lambda: clients.autoscaling.delete_namespaced_horizontal_pod_autoscaler(
            name,
            namespace,
        )
    )


def delete_pvc(clients: KubernetesClients, namespace: str, name: str) -> Any:
    """Delete a PVC and ignore already-deleted resources."""
    logger.debug("Deleting Kubernetes PVC namespace=%s name=%s.", namespace, name)
    return _delete_or_ignore_not_found(
        lambda: clients.core.delete_namespaced_persistent_volume_claim(name, namespace)
    )


def delete_secret(clients: KubernetesClients, namespace: str, name: str) -> Any:
    """Delete a Secret and ignore already-deleted resources."""
    logger.debug("Deleting Kubernetes Secret namespace=%s name=%s.", namespace, name)
    return _delete_or_ignore_not_found(
        lambda: clients.core.delete_namespaced_secret(name, namespace)
    )


def delete_namespace(clients: KubernetesClients, namespace: str) -> Any:
    """Delete a Namespace and ignore already-deleted resources."""
    logger.debug("Deleting Kubernetes Namespace name=%s.", namespace)
    return _delete_or_ignore_not_found(lambda: clients.core.delete_namespace(namespace))


def _create_or_patch(create: Any, patch: Any) -> Any:
    """Create a resource, patching when the Kubernetes API reports conflict."""
    try:
        return create()
    except Exception as exc:
        # 409 means the resource already exists. Patching makes worker retries
        # safe to repeat for the desired manifest.
        if _status_code(exc) == 409:
            logger.debug("Kubernetes create conflicted; patching existing resource.")
            return patch()
        raise


def _delete_or_ignore_not_found(delete: Any) -> Any:
    """Delete a resource while treating 404 as successful reconciliation."""
    try:
        return delete()
    except Exception as exc:
        # Delete jobs can be retried. If the resource is already gone, the
        # desired end state is satisfied.
        if _status_code(exc) == KUBERNETES_NOT_FOUND:
            logger.debug("Kubernetes delete ignored missing resource.")
            return None
        raise


def _status_code(exc: Exception) -> int | None:
    """Return Kubernetes ApiException status without importing the class."""
    return getattr(exc, "status", None)
