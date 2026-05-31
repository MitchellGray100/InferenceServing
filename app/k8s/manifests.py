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
LOCAL_GPU_DRIVER_VOLUME_NAME = "local-nvidia-driver-libs"
LOCAL_GPU_WSL_DEVICE_VOLUME_NAME = "local-wsl-gpu-device"


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
    # Normalize and validate resource names once, then reuse them throughout the
    # manifest so selectors, labels, and volumes stay aligned.
    names = names_from_deployment(deployment)
    namespace = names["k8s_namespace"]
    deployment_name = names["k8s_deployment_name"]
    service_name = names["k8s_service_name"]
    pvc = pvc_name or names["k8s_pvc_name"]
    labels = model_labels(namespace, service_name)
    container = build_vllm_container(deployment, pvc_name=pvc, secret_name=secret_name)

    pod_spec = {
        "containers": [container],
        "volumes": [
            {
                "name": "hf-cache",
                "persistentVolumeClaim": {
                    "claimName": pvc,
                },
            }
        ],
    }
    if should_mount_local_gpu_driver_libraries(deployment):
        # Docker Desktop/kind does not inject NVIDIA driver libraries into pods
        # the same way `docker run --gpus all` does. The local GPU smoke setup
        # copies those libraries into the kind node and this hostPath mount
        # exposes them to the vLLM container. Production clusters should rely
        # on the NVIDIA container runtime/device plugin path instead.
        pod_spec["volumes"].append(
            {
                "name": LOCAL_GPU_DRIVER_VOLUME_NAME,
                "hostPath": {
                    "path": Config.LOCAL_KIND_GPU_DRIVER_PATH,
                    "type": "Directory",
                },
            }
        )
        pod_spec["volumes"].append(
            {
                "name": LOCAL_GPU_WSL_DEVICE_VOLUME_NAME,
                "hostPath": {
                    "path": "/dev/dxg",
                    "type": "CharDevice",
                },
            }
        )

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
                # The Service selector and pod template labels must match or
                # traffic will never reach the vLLM pods.
                "matchLabels": model_selector_labels(service_name),
            },
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": {
                        "miniten.io/desired-generation": str(deployment["desired_generation"]),
                    },
                },
                "spec": pod_spec,
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
    # Build the names once so the Deployment, Service, HPA, PVC, and optional
    # Secret all refer to the same deterministic resources.
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
    if deployment["vllm_image"] == Config.K8S_SMOKE_TEST_IMAGE:
        return build_smoke_test_container(deployment, pvc_name=pvc_name)

    # vLLM exposes an OpenAI-compatible HTTP server. The deployment name is the
    # served model name clients use in `/v1/chat/completions`.
    args = [
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
    ]
    if deployment.get("gpu_count", 0) == 0 and Config.VLLM_CPU_MEMORY_UTILIZATION:
        # vLLM CPU defaults reserve most node memory for KV cache, which is too
        # aggressive for local kind/Docker Desktop. Keep this MiniTen-managed
        # setting internal instead of exposing raw vLLM memory flags to users.
        args.extend(
            [
                "--gpu-memory-utilization",
                str(Config.VLLM_CPU_MEMORY_UTILIZATION),
            ]
        )
    env = [
        {
            # Persist Hugging Face cache on the PVC so restarts do not
            # redownload model weights every time.
            "name": "HF_HOME",
            "value": HF_CACHE_MOUNT_PATH,
        }
    ]
    vllm_device = Config.VLLM_DEVICE or ("cuda" if deployment.get("gpu_count", 0) else "")
    if vllm_device:
        # Local kind clusters usually do not expose GPUs to pods. Letting vLLM
        # auto-detect the device can crash before readiness starts. vLLM reads
        # VLLM_TARGET_DEVICE while constructing CLI defaults. Newer CPU images
        # do not accept a `--device` CLI flag, so keep device selection in env.
        env.append(
            {
                "name": "VLLM_TARGET_DEVICE",
                "value": vllm_device,
            }
        )
    if Config.VLLM_LOGGING_LEVEL:
        env.append(
            {
                "name": "VLLM_LOGGING_LEVEL",
                "value": Config.VLLM_LOGGING_LEVEL,
            }
        )
    if should_mount_local_gpu_driver_libraries(deployment):
        env.append(
            {
                "name": "LD_LIBRARY_PATH",
                "value": (
                    f"{Config.LOCAL_KIND_GPU_DRIVER_PATH}:"
                    "/usr/local/nvidia/lib:/usr/local/nvidia/lib64:"
                    "/usr/local/cuda/lib64:"
                    "/usr/local/cuda/targets/x86_64-linux/lib:"
                    "/usr/local/lib:/usr/lib:/usr/lib/x86_64-linux-gnu"
                ),
            }
        )
        env.append(
            {
                "name": "NVIDIA_VISIBLE_DEVICES",
                "value": "all",
            }
        )
        env.append(
            {
                "name": "NVIDIA_DRIVER_CAPABILITIES",
                "value": "compute,utility",
            }
        )
        env.append(
            {
                "name": "CUDA_VISIBLE_DEVICES",
                "value": "0",
            }
        )

    container = {
        "name": "vllm",
        "image": deployment["vllm_image"],
        "imagePullPolicy": "IfNotPresent",
        "args": args,
        "ports": [
            {
                "name": VLLM_PORT_NAME,
                "containerPort": VLLM_PORT,
            }
        ],
        "env": env,
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
        # Private Hugging Face models use a Kubernetes Secret mounted as the
        # standard HF_TOKEN environment variable.
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

    if should_mount_local_gpu_driver_libraries(deployment):
        container["volumeMounts"].append(
            {
                "name": LOCAL_GPU_DRIVER_VOLUME_NAME,
                "mountPath": Config.LOCAL_KIND_GPU_DRIVER_PATH,
            }
        )
        container["volumeMounts"].append(
            {
                "name": LOCAL_GPU_WSL_DEVICE_VOLUME_NAME,
                "mountPath": "/dev/dxg",
            }
        )

    return container


def should_mount_local_gpu_driver_libraries(deployment: dict[str, Any]) -> bool:
    """Return true when local kind GPU smoke tests need driver hostPath mounts."""
    return bool(
        Config.LOCAL_KIND_GPU_DRIVER_MOUNT
        and deployment.get("gpu_count", 0)
        and deployment["vllm_image"] != Config.K8S_SMOKE_TEST_IMAGE
    )


def build_smoke_test_container(
    deployment: dict[str, Any],
    *,
    pvc_name: str,
) -> dict[str, Any]:
    """Build a tiny HTTP container for local Kubernetes smoke tests.

    Real model deployments use the vLLM container above. The smoke container is
    intentionally limited to the configured `K8S_SMOKE_TEST_IMAGE` so local
    cluster tests can validate Namespace/PVC/Deployment/Service/HPA/log plumbing
    without pulling or loading a large vLLM image.
    """
    server_code = (
        "import json\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'ok')\n"
        "    def do_POST(self):\n"
        "        length = int(self.headers.get('content-length', '0'))\n"
        "        body = json.loads(self.rfile.read(length) or b'{}')\n"
        "        payload = {'id': 'chatcmpl-smoke', 'object': 'chat.completion', 'model': body.get('model', 'smoke'), 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'smoke ok'}, 'finish_reason': 'stop'}]}\n"
        "        encoded = json.dumps(payload).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('content-type', 'application/json')\n"
        "        self.send_header('content-length', str(len(encoded)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(encoded)\n"
        "HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()\n"
    )
    return {
        "name": "vllm",
        "image": deployment["vllm_image"],
        "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-c", server_code],
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
            "initialDelaySeconds": 1,
            "periodSeconds": 2,
            "failureThreshold": 15,
        },
    }


def build_resource_requirements(deployment: dict[str, Any]) -> dict[str, Any]:
    """Build Kubernetes container requests/limits from deployment metadata."""
    # Drop unset CPU/memory values so Kubernetes receives only explicit
    # requests/limits from the deployment record.
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
        # NVIDIA's device plugin advertises GPUs through this extended resource.
        limits["nvidia.com/gpu"] = gpu_count

    return {
        "requests": requests,
        "limits": limits,
    }


def names_from_deployment(deployment: dict[str, Any]) -> dict[str, str]:
    """Return Kubernetes resource names for persisted and derived resources."""
    # Deployment, Service, and HPA names are stored with the deployment row.
    # PVC/Secret names are deterministic companion resources derived from the
    # namespace and project-local model name.
    derived_names = build_model_resource_names(
        deployment["k8s_namespace"],
        deployment["name"],
    )
    names = {
        "k8s_namespace": deployment["k8s_namespace"],
        "k8s_deployment_name": deployment["k8s_deployment_name"],
        "k8s_service_name": deployment["k8s_service_name"],
        "k8s_hpa_name": deployment["k8s_hpa_name"],
        "k8s_pvc_name": derived_names["k8s_pvc_name"],
        "k8s_secret_name": derived_names["k8s_secret_name"],
    }

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
    """Return a copy with unset resource fields removed."""
    return {key: value for key, value in values.items() if value is not None}
