"""Kubernetes manifest builders for vLLM deployments.

Manifest builders translate model deployment rows or deployment job payloads
into plain dictionaries. The deployment worker can pass these dictionaries to
the Kubernetes client, and tests can inspect them without a live cluster.
"""

from __future__ import annotations

from typing import Any

from app.config import Config
from app.k8s.names import build_model_resource_names, validate_dns_label


APP_LABEL = "miniten"
MANAGED_BY_LABEL = "miniten"
VLLM_PORT = 8000
VLLM_PORT_NAME = "http"
HF_CACHE_MOUNT_PATH = "/root/.cache/huggingface"


def build_namespace_manifest(namespace: str) -> dict[str, Any]:
    """Build a Namespace manifest for project isolation."""
    validate_dns_label(namespace, "namespace")
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": common_labels(namespace),
        },
    }


def build_pvc_manifest(
    namespace: str,
    pvc_name: str,
    *,
    size: str = Config.DEFAULT_PVC_SIZE,
) -> dict[str, Any]:
    """Build a PVC manifest used as the Hugging Face model cache."""
    validate_dns_label(namespace, "namespace")
    validate_dns_label(pvc_name, "pvc_name")
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": pvc_name,
            "namespace": namespace,
            "labels": common_labels(namespace),
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {
                "requests": {
                    "storage": size,
                },
            },
        },
    }


def build_deployment_manifest(
    deployment: dict[str, Any],
    *,
    pvc_name: str | None = None,
    secret_name: str | None = None,
) -> dict[str, Any]:
    """Build a Kubernetes Deployment manifest for a vLLM OpenAI server."""
    names = names_from_deployment(deployment)
    namespace = names["k8s_namespace"]
    deployment_name = names["k8s_deployment_name"]
    service_name = names["k8s_service_name"]
    pvc = pvc_name or names["k8s_pvc_name"]
    labels = model_labels(namespace, service_name)
    container = build_vllm_container(deployment, pvc_name=pvc, secret_name=secret_name)

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": deployment_name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "replicas": deployment["replicas"],
            "selector": {
                "matchLabels": model_selector_labels(service_name),
            },
            "template": {
                "metadata": {
                    "labels": labels,
                },
                "spec": {
                    "containers": [container],
                    "volumes": [
                        {
                            "name": "hf-cache",
                            "persistentVolumeClaim": {
                                "claimName": pvc,
                            },
                        }
                    ],
                },
            },
        },
    }


def build_service_manifest(deployment: dict[str, Any]) -> dict[str, Any]:
    """Build a stable ClusterIP Service for a named model deployment."""
    names = names_from_deployment(deployment)
    namespace = names["k8s_namespace"]
    service_name = names["k8s_service_name"]
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": service_name,
            "namespace": namespace,
            "labels": model_labels(namespace, service_name),
        },
        "spec": {
            "type": "ClusterIP",
            "selector": model_selector_labels(service_name),
            "ports": [
                {
                    "name": VLLM_PORT_NAME,
                    "port": VLLM_PORT,
                    "targetPort": VLLM_PORT_NAME,
                }
            ],
        },
    }


def build_hpa_manifest(deployment: dict[str, Any]) -> dict[str, Any] | None:
    """Build an autoscaling/v2 HPA manifest when autoscaling is enabled."""
    if not deployment.get("autoscaling_enabled"):
        return None

    names = names_from_deployment(deployment)
    namespace = names["k8s_namespace"]
    hpa_name = names["k8s_hpa_name"]
    deployment_name = names["k8s_deployment_name"]
    service_name = names["k8s_service_name"]
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {
            "name": hpa_name,
            "namespace": namespace,
            "labels": model_labels(namespace, service_name),
        },
        "spec": {
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": deployment_name,
            },
            "minReplicas": deployment["min_replicas"],
            "maxReplicas": deployment["max_replicas"],
            "metrics": [
                {
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {
                            "type": "Utilization",
                            "averageUtilization": deployment[
                                "target_cpu_utilization"
                            ],
                        },
                    },
                }
            ],
        },
    }


def build_secret_manifest(
    namespace: str,
    secret_name: str,
    *,
    hugging_face_token: str,
) -> dict[str, Any]:
    """Build an optional Secret manifest for private Hugging Face models."""
    validate_dns_label(namespace, "namespace")
    validate_dns_label(secret_name, "secret_name")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": namespace,
            "labels": common_labels(namespace),
        },
        "type": "Opaque",
        "stringData": {
            "HF_TOKEN": hugging_face_token,
        },
    }


def build_model_manifests(
    deployment: dict[str, Any],
    *,
    pvc_size: str = Config.DEFAULT_PVC_SIZE,
    hugging_face_token: str | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Build all resources required for one model deployment."""
    names = names_from_deployment(deployment)
    secret_name = names["k8s_secret_name"] if hugging_face_token else None
    return {
        "namespace": build_namespace_manifest(names["k8s_namespace"]),
        "pvc": build_pvc_manifest(
            names["k8s_namespace"],
            names["k8s_pvc_name"],
            size=pvc_size,
        ),
        "secret": build_secret_manifest(
            names["k8s_namespace"],
            names["k8s_secret_name"],
            hugging_face_token=hugging_face_token,
        )
        if hugging_face_token
        else None,
        "deployment": build_deployment_manifest(
            deployment,
            pvc_name=names["k8s_pvc_name"],
            secret_name=secret_name,
        ),
        "service": build_service_manifest(deployment),
        "hpa": build_hpa_manifest(deployment),
    }


def build_vllm_container(
    deployment: dict[str, Any],
    *,
    pvc_name: str,
    secret_name: str | None = None,
) -> dict[str, Any]:
    """Build the single vLLM container spec for a Deployment."""
    container = {
        "name": "vllm",
        "image": deployment["vllm_image"],
        "args": [
            "--model",
            deployment["model_id"],
            "--served-model-name",
            deployment["name"],
            "--host",
            "0.0.0.0",
            "--port",
            str(VLLM_PORT),
            "--dtype",
            deployment["vllm_dtype"],
            "--max-model-len",
            str(deployment["vllm_max_model_len"]),
        ],
        "ports": [
            {
                "name": VLLM_PORT_NAME,
                "containerPort": VLLM_PORT,
            }
        ],
        "env": [
            {
                "name": "HF_HOME",
                "value": HF_CACHE_MOUNT_PATH,
            }
        ],
        "resources": build_resource_requirements(deployment),
        "volumeMounts": [
            {
                "name": "hf-cache",
                "mountPath": HF_CACHE_MOUNT_PATH,
            }
        ],
        "readinessProbe": {
            "httpGet": {
                "path": "/health",
                "port": VLLM_PORT_NAME,
            },
            "initialDelaySeconds": 10,
            "periodSeconds": 10,
            "failureThreshold": 30,
        },
    }

    if secret_name:
        container["env"].append(
            {
                "name": "HF_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": secret_name,
                        "key": "HF_TOKEN",
                    }
                },
            }
        )

    return container


def build_resource_requirements(deployment: dict[str, Any]) -> dict[str, Any]:
    """Build Kubernetes container requests/limits from deployment metadata."""
    requests = _without_none(
        {
            "cpu": deployment.get("cpu_request"),
            "memory": deployment.get("memory_request"),
        }
    )
    limits = _without_none(
        {
            "cpu": deployment.get("cpu_limit"),
            "memory": deployment.get("memory_limit"),
        }
    )
    gpu_count = deployment.get("gpu_count", 0)

    if gpu_count:
        limits["nvidia.com/gpu"] = gpu_count

    return {
        "requests": requests,
        "limits": limits,
    }


def names_from_deployment(deployment: dict[str, Any]) -> dict[str, str]:
    """Return Kubernetes names from metadata, deriving optional names as needed."""
    names = build_model_resource_names(
        deployment["k8s_namespace"],
        deployment["name"],
    )
    names["k8s_deployment_name"] = deployment.get(
        "k8s_deployment_name",
        names["k8s_deployment_name"],
    )
    names["k8s_service_name"] = deployment.get(
        "k8s_service_name",
        names["k8s_service_name"],
    )
    names["k8s_hpa_name"] = deployment.get("k8s_hpa_name", names["k8s_hpa_name"])

    for field, value in names.items():
        validate_dns_label(value, field)

    return names


def common_labels(namespace: str) -> dict[str, str]:
    """Labels shared by MiniTen-owned Kubernetes resources."""
    return {
        "app.kubernetes.io/name": APP_LABEL,
        "app.kubernetes.io/managed-by": MANAGED_BY_LABEL,
        "miniten.io/project-namespace": namespace,
    }


def model_selector_labels(model_name: str) -> dict[str, str]:
    """Labels used by the Service selector and Deployment pod template."""
    return {
        "app.kubernetes.io/name": APP_LABEL,
        "miniten.io/model": model_name,
    }


def model_labels(namespace: str, model_name: str) -> dict[str, str]:
    """Return common resource labels plus model selector labels."""
    return {
        **common_labels(namespace),
        **model_selector_labels(model_name),
    }


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
