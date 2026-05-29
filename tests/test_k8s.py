"""Kubernetes naming, manifest, and client helper tests."""

import pytest

from app.config import Config
from app.k8s import client as k8s_client
from app.k8s.deployment_manager import (
    apply_model_deployment,
    delete_model_deployment,
    inspect_model_readiness,
    read_model_logs,
    scale_model_deployment,
)
from app.k8s.manifests import (
    HF_CACHE_MOUNT_PATH,
    VLLM_PORT,
    build_deployment_manifest,
    build_hpa_manifest,
    build_model_manifests,
    build_pvc_manifest,
    build_service_manifest,
)
from app.k8s.names import (
    MODEL_GENERATION_SUFFIX,
    append_suffix,
    build_model_resource_names,
    validate_dns_label,
)
from app.utils.errors import ApiError


def deployment_payload(autoscaling_enabled: bool = True) -> dict[str, object]:
    return {
        "project_id": "a2fc41b7-862e-4060-b466-2376f29227bb",
        "model_deployment_id": "bf3dc090-5bb4-46f6-964d-6cd8375ddf56",
        "name": "qwen-small-prod",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "k8s_namespace": "miniten-personal",
        "k8s_deployment_name": "qwen-small-prod-v1",
        "k8s_service_name": "qwen-small-prod",
        "k8s_hpa_name": "qwen-small-prod-v1",
        "replicas": 1,
        "cpu_request": "2",
        "cpu_limit": "4",
        "memory_request": "8Gi",
        "memory_limit": "16Gi",
        "gpu_count": 0,
        "vllm_image": "vllm/vllm-openai:latest",
        "vllm_dtype": "auto",
        "vllm_max_model_len": 4096,
        "autoscaling_enabled": autoscaling_enabled,
        "min_replicas": 1 if autoscaling_enabled else None,
        "max_replicas": 3 if autoscaling_enabled else None,
        "target_cpu_utilization": 70 if autoscaling_enabled else None,
    }


def test_validate_dns_label_rejects_invalid_names() -> None:
    assert validate_dns_label("qwen-small-prod") == "qwen-small-prod"

    with pytest.raises(ApiError):
        validate_dns_label("Qwen Small Prod")


def test_append_suffix_preserves_dns_label_length() -> None:
    base = "a" * 63
    value = append_suffix(base, MODEL_GENERATION_SUFFIX)

    assert value.endswith(MODEL_GENERATION_SUFFIX)
    assert len(value) <= 63


def test_build_model_resource_names() -> None:
    names = build_model_resource_names("miniten-personal", "qwen-small-prod")

    assert names["k8s_deployment_name"] == "qwen-small-prod-v1"
    assert names["k8s_service_name"] == "qwen-small-prod"
    assert names["k8s_pvc_name"] == "qwen-small-prod-hf-cache"
    assert names["k8s_secret_name"] == "qwen-small-prod-secrets"


def test_build_pvc_manifest() -> None:
    manifest = build_pvc_manifest("miniten-personal", "qwen-small-prod-hf-cache")

    assert manifest["kind"] == "PersistentVolumeClaim"
    assert manifest["metadata"]["namespace"] == "miniten-personal"
    assert manifest["spec"]["resources"]["requests"]["storage"] == "20Gi"


def test_build_deployment_manifest() -> None:
    manifest = build_deployment_manifest(deployment_payload())
    container = manifest["spec"]["template"]["spec"]["containers"][0]

    assert manifest["kind"] == "Deployment"
    assert manifest["metadata"]["name"] == "qwen-small-prod-v1"
    assert manifest["spec"]["replicas"] == 1
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert "--model" in container["args"]
    assert "Qwen/Qwen2.5-0.5B-Instruct" in container["args"]
    assert "--gpu-memory-utilization" in container["args"]
    assert "0.15" in container["args"]
    assert container["ports"][0]["containerPort"] == VLLM_PORT
    assert container["volumeMounts"][0]["mountPath"] == HF_CACHE_MOUNT_PATH
    assert container["resources"]["requests"]["cpu"] == "2"
    assert manifest["spec"]["template"]["metadata"]["annotations"] == {
        "miniten.io/desired-generation": "1"
    }


def test_build_deployment_manifest_changes_pod_template_for_new_generation() -> None:
    payload = deployment_payload()
    payload["desired_generation"] = 7

    manifest = build_deployment_manifest(payload)

    assert (
        manifest["spec"]["template"]["metadata"]["annotations"][
            "miniten.io/desired-generation"
        ]
        == "7"
    )


def test_build_deployment_manifest_with_gpu_and_secret() -> None:
    payload = deployment_payload()
    payload["gpu_count"] = 1

    manifest = build_deployment_manifest(payload, secret_name="qwen-small-prod-secrets")
    container = manifest["spec"]["template"]["spec"]["containers"][0]

    assert container["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert "--gpu-memory-utilization" not in container["args"]
    assert {"name": "VLLM_TARGET_DEVICE", "value": "cuda"} in container["env"]
    assert container["env"][-1]["valueFrom"]["secretKeyRef"]["name"] == (
        "qwen-small-prod-secrets"
    )


def test_build_deployment_manifest_with_local_gpu_driver_mount(monkeypatch) -> None:
    """Local kind GPU smoke tests can mount copied NVIDIA driver libraries."""
    payload = deployment_payload()
    payload["gpu_count"] = 1
    monkeypatch.setattr(Config, "LOCAL_KIND_GPU_DRIVER_MOUNT", True)
    monkeypatch.setattr(Config, "LOCAL_KIND_GPU_DRIVER_PATH", "/usr/local/nvidia/lib64")

    manifest = build_deployment_manifest(payload)
    pod_spec = manifest["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    assert {
        "name": "LD_LIBRARY_PATH",
        "value": (
            "/usr/local/nvidia/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:"
            "/usr/local/cuda/lib64:/usr/local/cuda/targets/x86_64-linux/lib:"
            "/usr/local/lib:/usr/lib:/usr/lib/x86_64-linux-gnu"
        ),
    } in container["env"]
    assert {"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"} in container["env"]
    assert {
        "name": "NVIDIA_DRIVER_CAPABILITIES",
        "value": "compute,utility",
    } in container["env"]
    assert {"name": "CUDA_VISIBLE_DEVICES", "value": "0"} in container["env"]
    assert {
        "name": "local-nvidia-driver-libs",
        "hostPath": {
            "path": "/usr/local/nvidia/lib64",
            "type": "Directory",
        },
    } in pod_spec["volumes"]
    assert {
        "name": "local-wsl-gpu-device",
        "hostPath": {
            "path": "/dev/dxg",
            "type": "CharDevice",
        },
    } in pod_spec["volumes"]
    assert {
        "name": "local-wsl-gpu-device",
        "mountPath": "/dev/dxg",
    } in container["volumeMounts"]


def test_build_deployment_manifest_with_vllm_device(monkeypatch) -> None:
    monkeypatch.setattr(Config, "VLLM_DEVICE", "cpu")
    monkeypatch.setattr(Config, "VLLM_LOGGING_LEVEL", "DEBUG")

    manifest = build_deployment_manifest(deployment_payload())
    container = manifest["spec"]["template"]["spec"]["containers"][0]

    assert "--device" not in container["args"]
    assert {"name": "VLLM_TARGET_DEVICE", "value": "cpu"} in container["env"]
    assert {"name": "VLLM_LOGGING_LEVEL", "value": "DEBUG"} in container["env"]


def test_build_deployment_manifest_with_smoke_test_image(monkeypatch) -> None:
    payload = deployment_payload()
    payload["vllm_image"] = "python:3.12-alpine"
    monkeypatch.setattr(Config, "K8S_SMOKE_TEST_IMAGE", "python:3.12-alpine")

    manifest = build_deployment_manifest(payload)
    container = manifest["spec"]["template"]["spec"]["containers"][0]

    assert container["command"][0] == "python"
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["readinessProbe"]["httpGet"]["path"] == "/health"


def test_build_service_manifest() -> None:
    manifest = build_service_manifest(deployment_payload())

    assert manifest["kind"] == "Service"
    assert manifest["metadata"]["name"] == "qwen-small-prod"
    assert manifest["spec"]["ports"][0]["targetPort"] == "http"
    assert manifest["spec"]["selector"]["miniten.io/model"] == "qwen-small-prod"


def test_build_hpa_manifest() -> None:
    manifest = build_hpa_manifest(deployment_payload())

    assert manifest is not None
    assert manifest["kind"] == "HorizontalPodAutoscaler"
    assert manifest["spec"]["minReplicas"] == 1
    assert manifest["spec"]["maxReplicas"] == 3
    assert manifest["spec"]["metrics"][0]["resource"]["target"][
        "averageUtilization"
    ] == 70


def test_build_hpa_manifest_returns_none_when_disabled() -> None:
    assert build_hpa_manifest(deployment_payload(False)) is None


def test_build_model_manifests() -> None:
    manifests = build_model_manifests(
        deployment_payload(),
        hugging_face_token="hf_example",
    )

    assert manifests["namespace"]["kind"] == "Namespace"
    assert manifests["pvc"]["kind"] == "PersistentVolumeClaim"
    assert manifests["secret"]["stringData"]["HF_TOKEN"] == "hf_example"
    assert manifests["deployment"]["kind"] == "Deployment"
    assert manifests["service"]["kind"] == "Service"
    assert manifests["hpa"]["kind"] == "HorizontalPodAutoscaler"


def test_client_apply_namespace_patches_on_conflict() -> None:
    core = FakeCore(conflict_on_create=True)
    clients = k8s_client.KubernetesClients(core=core, apps=FakeApps(), autoscaling=FakeHpa())
    manifest = {"metadata": {"name": "miniten-personal"}}

    k8s_client.apply_namespace(clients, manifest)

    assert core.calls == ["create_namespace", "patch_namespace"]


def test_client_delete_ignores_not_found() -> None:
    core = FakeCore(not_found_on_delete=True)
    clients = k8s_client.KubernetesClients(core=core, apps=FakeApps(), autoscaling=FakeHpa())

    assert k8s_client.delete_service(clients, "miniten-personal", "missing") is None


def test_client_delete_namespace_ignores_not_found() -> None:
    core = FakeCore(not_found_on_delete=True)
    clients = k8s_client.KubernetesClients(core=core, apps=FakeApps(), autoscaling=FakeHpa())

    assert k8s_client.delete_namespace(clients, "miniten-personal") is None
    assert "delete_namespace" in core.calls


def test_deployment_manager_apply_order() -> None:
    clients = k8s_client.KubernetesClients(
        core=FakeCore(),
        apps=FakeApps(),
        autoscaling=FakeHpa(),
    )

    apply_model_deployment(clients, deployment_payload())

    assert clients.core.calls[:2] == [
        "create_namespace",
        "create_namespaced_persistent_volume_claim",
    ]
    assert clients.apps.calls == [
        "create_namespaced_deployment",
        "read_namespaced_deployment",
    ]
    assert clients.autoscaling.calls == [
        "create_namespaced_horizontal_pod_autoscaler",
        "read_namespaced_horizontal_pod_autoscaler",
    ]


def test_deployment_manager_apply_secret_and_no_hpa() -> None:
    clients = k8s_client.KubernetesClients(
        core=FakeCore(),
        apps=FakeApps(),
        autoscaling=FakeHpa(),
    )

    apply_model_deployment(
        clients,
        deployment_payload(autoscaling_enabled=False),
        hugging_face_token="hf_example",
    )

    assert "create_namespaced_secret" in clients.core.calls
    assert clients.autoscaling.calls == []


def test_deployment_manager_delete_with_cache_and_secret() -> None:
    clients = k8s_client.KubernetesClients(
        core=FakeCore(),
        apps=FakeApps(),
        autoscaling=FakeHpa(),
    )

    delete_model_deployment(clients, deployment_payload(), delete_cache=True)

    assert clients.autoscaling.calls == [
        "delete_namespaced_horizontal_pod_autoscaler"
    ]
    assert "delete_namespaced_service" in clients.core.calls
    assert "delete_namespaced_secret" in clients.core.calls
    assert "delete_namespaced_persistent_volume_claim" in clients.core.calls
    assert clients.apps.calls == ["delete_namespaced_deployment"]


def test_deployment_manager_delete_can_skip_secret_and_cache() -> None:
    clients = k8s_client.KubernetesClients(
        core=FakeCore(),
        apps=FakeApps(),
        autoscaling=FakeHpa(),
    )

    delete_model_deployment(
        clients,
        deployment_payload(),
        delete_cache=False,
        delete_secret=False,
    )

    assert "delete_namespaced_secret" not in clients.core.calls
    assert "delete_namespaced_persistent_volume_claim" not in clients.core.calls


def test_deployment_manager_scale() -> None:
    apps = FakeApps(deployment_status=ready_deployment_status(available_replicas=3))
    clients = k8s_client.KubernetesClients(
        core=FakeCore(pods=[ready_pod("pod-a"), ready_pod("pod-b"), ready_pod("pod-c")]),
        apps=apps,
        autoscaling=FakeHpa(),
    )

    scale_model_deployment(clients, deployment_payload(), 3)

    assert apps.scale_body == {"spec": {"replicas": 3}}


def test_inspect_model_readiness_ready() -> None:
    clients = k8s_client.KubernetesClients(
        core=FakeCore(pods=[ready_pod()]),
        apps=FakeApps(deployment_status=ready_deployment_status()),
        autoscaling=FakeHpa(),
    )

    status = inspect_model_readiness(clients, deployment_payload(), expected_replicas=1)

    assert status["ready"] is True
    assert status["scheduled_pods"] == 1
    assert status["ready_pods"] == 1
    assert status["pods"][0]["name"] == "pod-a"


def test_inspect_model_readiness_detects_image_pull_failure() -> None:
    clients = k8s_client.KubernetesClients(
        core=FakeCore(pods=[waiting_pod("ImagePullBackOff")]),
        apps=FakeApps(deployment_status=ready_deployment_status()),
        autoscaling=FakeHpa(),
    )

    status = inspect_model_readiness(clients, deployment_payload(), expected_replicas=1)

    assert status["ready"] is False
    assert status["failed"] is True
    assert "ImagePullBackOff" in status["reason"]


def test_read_model_logs_returns_one_entry_per_pod() -> None:
    core = FakeCore(pods=[ready_pod("pod-a"), ready_pod("pod-b")])
    clients = k8s_client.KubernetesClients(
        core=core,
        apps=FakeApps(deployment_status=ready_deployment_status()),
        autoscaling=FakeHpa(),
    )

    logs = read_model_logs(clients, deployment_payload(), tail_lines=20)

    assert logs == [
        {"pod": "pod-a", "text": "logs for pod-a"},
        {"pod": "pod-b", "text": "logs for pod-b"},
    ]
    assert core.log_tail_lines == [20, 20]


def test_read_model_logs_returns_error_text_when_pod_log_read_fails() -> None:
    core = FakeCore(pods=[ready_pod("pod-a")], fail_log_read=True)
    clients = k8s_client.KubernetesClients(
        core=core,
        apps=FakeApps(deployment_status=ready_deployment_status()),
        autoscaling=FakeHpa(),
    )

    logs = read_model_logs(clients, deployment_payload(), tail_lines=20)

    assert logs[0]["pod"] == "pod-a"
    assert logs[0]["text"].startswith("Failed to read pod logs:")


def test_apply_secret_and_hpa_patch_on_conflict() -> None:
    core = FakeCore(conflict_on_create=True)
    hpa = FakeHpa(conflict_on_create=True)
    clients = k8s_client.KubernetesClients(core=core, apps=FakeApps(), autoscaling=hpa)

    k8s_client.apply_secret(
        clients,
        {"metadata": {"name": "secret", "namespace": "ns"}},
    )
    k8s_client.apply_hpa(
        clients,
        {"metadata": {"name": "hpa", "namespace": "ns"}},
    )

    assert core.calls == [
        "create_namespaced_secret",
        "patch_namespaced_secret",
    ]
    assert hpa.calls == [
        "create_namespaced_horizontal_pod_autoscaler",
        "patch_namespaced_horizontal_pod_autoscaler",
    ]


def test_delete_hpa_pvc_and_secret() -> None:
    clients = k8s_client.KubernetesClients(
        core=FakeCore(),
        apps=FakeApps(),
        autoscaling=FakeHpa(),
    )

    k8s_client.delete_hpa(clients, "ns", "hpa")
    k8s_client.delete_pvc(clients, "ns", "pvc")
    k8s_client.delete_secret(clients, "ns", "secret")

    assert clients.autoscaling.calls == [
        "delete_namespaced_horizontal_pod_autoscaler"
    ]
    assert "delete_namespaced_persistent_volume_claim" in clients.core.calls
    assert "delete_namespaced_secret" in clients.core.calls


class FakeApiException(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


def ready_deployment_status(available_replicas: int = 1) -> dict[str, object]:
    return {
        "status": {
            "available_replicas": available_replicas,
            "conditions": [{"type": "Available", "status": "True"}],
        }
    }


def ready_pod(name: str = "pod-a") -> dict[str, object]:
    return {
        "metadata": {"name": name},
        "status": {
            "conditions": [
                {"type": "PodScheduled", "status": "True"},
                {"type": "Ready", "status": "True"},
            ],
            "container_statuses": [],
        },
    }


def waiting_pod(reason: str) -> dict[str, object]:
    pod = ready_pod()
    pod["status"]["conditions"] = [{"type": "PodScheduled", "status": "True"}]
    pod["status"]["container_statuses"] = [
        {"state": {"waiting": {"reason": reason, "message": "pull failed"}}}
    ]
    return pod


class FakeCore:
    def __init__(
        self,
        *,
        conflict_on_create: bool = False,
        not_found_on_delete: bool = False,
        pods=None,
        fail_log_read: bool = False,
    ):
        self.conflict_on_create = conflict_on_create
        self.not_found_on_delete = not_found_on_delete
        self.pods = pods or [ready_pod()]
        self.fail_log_read = fail_log_read
        self.calls: list[str] = []
        self.log_tail_lines: list[int] = []

    def create_namespace(self, manifest):
        self.calls.append("create_namespace")
        if self.conflict_on_create:
            raise FakeApiException(409)
        return manifest

    def patch_namespace(self, name, manifest):
        self.calls.append("patch_namespace")
        return manifest

    def create_namespaced_persistent_volume_claim(self, namespace, manifest):
        self.calls.append("create_namespaced_persistent_volume_claim")
        if self.conflict_on_create:
            raise FakeApiException(409)
        return manifest

    def patch_namespaced_persistent_volume_claim(self, name, namespace, manifest):
        self.calls.append("patch_namespaced_persistent_volume_claim")
        return manifest

    def create_namespaced_secret(self, namespace, manifest):
        self.calls.append("create_namespaced_secret")
        if self.conflict_on_create:
            raise FakeApiException(409)
        return manifest

    def patch_namespaced_secret(self, name, namespace, manifest):
        self.calls.append("patch_namespaced_secret")
        return manifest

    def create_namespaced_service(self, namespace, manifest):
        self.calls.append("create_namespaced_service")
        if self.conflict_on_create:
            raise FakeApiException(409)
        return manifest

    def patch_namespaced_service(self, name, namespace, manifest):
        self.calls.append("patch_namespaced_service")
        return manifest

    def read_namespaced_service(self, name, namespace):
        self.calls.append("read_namespaced_service")
        return {"metadata": {"name": name, "namespace": namespace}}

    def read_namespaced_persistent_volume_claim(self, name, namespace):
        self.calls.append("read_namespaced_persistent_volume_claim")
        return {"metadata": {"name": name, "namespace": namespace}}

    def read_namespaced_secret(self, name, namespace):
        self.calls.append("read_namespaced_secret")
        return {"metadata": {"name": name, "namespace": namespace}}

    def list_namespaced_pod(self, namespace, label_selector):
        self.calls.append("list_namespaced_pod")
        return type("PodList", (), {"items": self.pods})()

    def read_namespaced_pod_log(self, name, namespace, tail_lines, _request_timeout=None):
        self.calls.append("read_namespaced_pod_log")
        self.log_tail_lines.append(tail_lines)
        if self.fail_log_read:
            raise RuntimeError("logs unavailable")
        return f"logs for {name}"

    def delete_namespaced_service(self, name, namespace):
        self.calls.append("delete_namespaced_service")
        if self.not_found_on_delete:
            raise FakeApiException(404)
        return None

    def delete_namespaced_persistent_volume_claim(self, name, namespace):
        self.calls.append("delete_namespaced_persistent_volume_claim")
        return None

    def delete_namespaced_secret(self, name, namespace):
        self.calls.append("delete_namespaced_secret")
        return None

    def delete_namespace(self, namespace):
        self.calls.append("delete_namespace")
        if self.not_found_on_delete:
            raise FakeApiException(404)
        return None


class FakeApps:
    def __init__(self, *, deployment_status=None):
        self.calls: list[str] = []
        self.scale_body = None
        self.deployment_status = deployment_status or ready_deployment_status()

    def create_namespaced_deployment(self, namespace, manifest):
        self.calls.append("create_namespaced_deployment")
        return manifest

    def patch_namespaced_deployment(self, name, namespace, manifest):
        self.calls.append("patch_namespaced_deployment")
        return manifest

    def read_namespaced_deployment(self, name, namespace):
        self.calls.append("read_namespaced_deployment")
        return self.deployment_status

    def delete_namespaced_deployment(self, name, namespace):
        self.calls.append("delete_namespaced_deployment")
        return None

    def patch_namespaced_deployment_scale(self, name, namespace, body):
        self.calls.append("patch_namespaced_deployment_scale")
        self.scale_body = body
        return body


class FakeHpa:
    def __init__(self, *, conflict_on_create: bool = False):
        self.conflict_on_create = conflict_on_create
        self.calls: list[str] = []

    def create_namespaced_horizontal_pod_autoscaler(self, namespace, manifest):
        self.calls.append("create_namespaced_horizontal_pod_autoscaler")
        if self.conflict_on_create:
            raise FakeApiException(409)
        return manifest

    def patch_namespaced_horizontal_pod_autoscaler(self, name, namespace, manifest):
        self.calls.append("patch_namespaced_horizontal_pod_autoscaler")
        return manifest

    def read_namespaced_horizontal_pod_autoscaler(self, name, namespace):
        self.calls.append("read_namespaced_horizontal_pod_autoscaler")
        return {"metadata": {"name": name, "namespace": namespace}}

    def delete_namespaced_horizontal_pod_autoscaler(self, name, namespace):
        self.calls.append("delete_namespaced_horizontal_pod_autoscaler")
        return None
