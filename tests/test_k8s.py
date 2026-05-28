"""Kubernetes naming, manifest, and client helper tests."""

import pytest

from app.k8s import client as k8s_client
from app.k8s.deployment_manager import (
    apply_model_deployment,
    delete_model_deployment,
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
    assert "--model" in container["args"]
    assert "Qwen/Qwen2.5-0.5B-Instruct" in container["args"]
    assert container["ports"][0]["containerPort"] == VLLM_PORT
    assert container["volumeMounts"][0]["mountPath"] == HF_CACHE_MOUNT_PATH
    assert container["resources"]["requests"]["cpu"] == "2"


def test_build_deployment_manifest_with_gpu_and_secret() -> None:
    payload = deployment_payload()
    payload["gpu_count"] = 1

    manifest = build_deployment_manifest(payload, secret_name="qwen-small-prod-secrets")
    container = manifest["spec"]["template"]["spec"]["containers"][0]

    assert container["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert container["env"][-1]["valueFrom"]["secretKeyRef"]["name"] == (
        "qwen-small-prod-secrets"
    )


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
    assert clients.apps.calls == ["create_namespaced_deployment"]
    assert clients.autoscaling.calls == [
        "create_namespaced_horizontal_pod_autoscaler"
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
    apps = FakeApps()
    clients = k8s_client.KubernetesClients(core=FakeCore(), apps=apps, autoscaling=FakeHpa())

    scale_model_deployment(clients, deployment_payload(), 3)

    assert apps.scale_body == {"spec": {"replicas": 3}}


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


class FakeCore:
    def __init__(
        self,
        *,
        conflict_on_create: bool = False,
        not_found_on_delete: bool = False,
    ):
        self.conflict_on_create = conflict_on_create
        self.not_found_on_delete = not_found_on_delete
        self.calls: list[str] = []

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


class FakeApps:
    def __init__(self):
        self.calls: list[str] = []
        self.scale_body = None

    def create_namespaced_deployment(self, namespace, manifest):
        self.calls.append("create_namespaced_deployment")
        return manifest

    def patch_namespaced_deployment(self, name, namespace, manifest):
        self.calls.append("patch_namespaced_deployment")
        return manifest

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

    def delete_namespaced_horizontal_pod_autoscaler(self, name, namespace):
        self.calls.append("delete_namespaced_horizontal_pod_autoscaler")
        return None
