"""Model deployment route and service helper tests."""

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from app import create_app
from app.security.tokens import create_access_token
from app.services import model_deployment_service
from app.services.model_deployment_service import (
    build_job_payload,
    build_k8s_names,
    get_model_deployment_status,
    model_deployment_not_found_error,
    serialize_deployment_job,
    serialize_model_deployment,
    validate_deployment_spec,
)
from app.utils.errors import ApiError


PROJECT_ID = "a2fc41b7-862e-4060-b466-2376f29227bb"
MODEL_DEPLOYMENT_ID = "bf3dc090-5bb4-46f6-964d-6cd8375ddf56"
DEPLOYMENT_JOB_ID = "3ef7d993-cb61-4392-b36b-2ed2e1d88af1"
USER_ID = "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e"


class TestConfig:
    TESTING = True
    API_DEBUG = True
    SECRET_KEY = "test-secret-key-change-me-32-bytes"
    DEFAULT_MODEL_REPLICAS = 1
    DEFAULT_HPA_MIN_REPLICAS = 1
    DEFAULT_HPA_MAX_REPLICAS = 3
    DEFAULT_HPA_TARGET_CPU_UTILIZATION = 70
    VLLM_IMAGE = "vllm/vllm-openai:latest"
    VLLM_CPU_IMAGE = "vllm/vllm-openai-cpu:latest-x86_64"
    K8S_SMOKE_TEST_IMAGE = "python:3.12-alpine"
    K8S_SMOKE_TEST_MODEL_ID = "miniten/smoke-openai-compatible"


@pytest.fixture
def app():
    return create_app(TestConfig)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_headers(app):
    with app.app_context():
        token = create_access_token(USER_ID)
    return {"Authorization": f"Bearer {token}"}


def idempotency_headers(auth_headers, key: str = "test-idempotency-key"):
    return {**auth_headers, "Idempotency-Key": key}


def bypass_idempotency(monkeypatch):
    monkeypatch.setattr(
        "app.routes.model_deployments.idempotency_service."
        "run_idempotent_control_plane_request",
        lambda **kwargs: kwargs["action"](),
    )


def model_deployment_response(status: str = "deploying") -> dict[str, object]:
    return {
        "modelDeploymentID": MODEL_DEPLOYMENT_ID,
        "projectID": PROJECT_ID,
        "name": "qwen-small-prod",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "status": status,
        "k8s_namespace": "miniten-personal",
        "k8s_deployment_name": "qwen-small-prod-v1",
        "k8s_service_name": "qwen-small-prod",
        "k8s_hpa_name": "qwen-small-prod-v1",
        "replicas": 1,
        "desired_generation": 1,
        "resources": {
            "cpu_request": "2",
            "cpu_limit": "4",
            "memory_request": "8Gi",
            "memory_limit": "16Gi",
            "gpu_count": 0,
        },
        "vllm": {
            "image": "vllm/vllm-openai:latest",
            "dtype": "auto",
            "max_model_len": 4096,
        },
        "autoscaling": {
            "enabled": True,
            "min_replicas": 1,
            "max_replicas": 3,
            "target_cpu_utilization": 70,
        },
        "created_at": "2026-05-17T12:00:00Z",
        "updated_at": "2026-05-17T12:00:00Z",
        "deleted_at": None,
    }


def deployment_job_response(job_type: str = "deploy_model") -> dict[str, object]:
    return {
        "deploymentJobID": DEPLOYMENT_JOB_ID,
        "projectID": PROJECT_ID,
        "modelDeploymentID": MODEL_DEPLOYMENT_ID,
        "job_type": job_type,
        "desired_generation": 1,
        "status": "queued",
        "attempts": 0,
        "max_attempts": 3,
        "last_error": None,
        "created_at": "2026-05-17T12:00:00Z",
        "updated_at": "2026-05-17T12:00:00Z",
    }


def command_response(job_type: str = "deploy_model") -> dict[str, object]:
    return {
        "modelDeployment": model_deployment_response(),
        "deploymentJob": deployment_job_response(job_type),
    }


def test_create_model_deployment_route(monkeypatch, client, auth_headers) -> None:
    bypass_idempotency(monkeypatch)

    def create_model_deployment(user_id, project_id, data):
        assert user_id == USER_ID
        assert project_id == PROJECT_ID
        assert data["name"] == "qwen-small-prod"
        return command_response()

    monkeypatch.setattr(
        "app.routes.model_deployments.model_deployment_service."
        "create_model_deployment",
        create_model_deployment,
    )

    response = client.post(
        f"/v1/projects/{PROJECT_ID}/models",
        json={"name": "qwen-small-prod", "model_id": "Qwen/Qwen2.5-0.5B-Instruct"},
        headers=idempotency_headers(auth_headers, "create-model"),
    )

    assert response.status_code == 201
    assert response.get_json() == command_response()


def test_list_model_deployments_route(monkeypatch, client, auth_headers) -> None:
    expected = {"modelDeployments": [model_deployment_response()]}
    monkeypatch.setattr(
        "app.routes.model_deployments.model_deployment_service."
        "list_model_deployments",
        lambda user_id, project_id: expected,
    )

    response = client.get(f"/v1/projects/{PROJECT_ID}/models", headers=auth_headers)

    assert response.status_code == 200
    assert response.get_json() == expected


def test_get_model_deployment_route(monkeypatch, client, auth_headers) -> None:
    monkeypatch.setattr(
        "app.routes.model_deployments.model_deployment_service."
        "get_model_deployment",
        lambda user_id, project_id, model_deployment_id: model_deployment_response(),
    )

    response = client.get(
        f"/v1/projects/{PROJECT_ID}/models/{MODEL_DEPLOYMENT_ID}",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == model_deployment_response()


def test_list_model_deployment_jobs_route(monkeypatch, client, auth_headers) -> None:
    expected = {"deploymentJobs": [deployment_job_response()]}
    monkeypatch.setattr(
        "app.routes.model_deployments.model_deployment_service."
        "list_model_deployment_jobs",
        lambda user_id, project_id, model_deployment_id: expected,
    )

    response = client.get(
        f"/v1/projects/{PROJECT_ID}/models/{MODEL_DEPLOYMENT_ID}/jobs",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == expected


def test_get_model_deployment_status_route(monkeypatch, client, auth_headers) -> None:
    expected = {
        "modelDeployment": model_deployment_response(),
        "latestDeploymentJob": deployment_job_response(),
        "recentDeploymentJobs": [deployment_job_response()],
        "kubernetes": {"available": False, "reason": "offline"},
    }
    monkeypatch.setattr(
        "app.routes.model_deployments.model_deployment_service."
        "get_model_deployment_status",
        lambda user_id, project_id, model_deployment_id: expected,
    )

    response = client.get(
        f"/v1/projects/{PROJECT_ID}/models/{MODEL_DEPLOYMENT_ID}/status",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == expected


def test_list_model_logs_route(monkeypatch, client, auth_headers) -> None:
    expected = {"model": model_deployment_response(), "logs": [{"pod": "pod-a", "text": "ok"}]}

    def list_model_logs(user_id, project_id, model_name, tail):
        assert user_id == USER_ID
        assert project_id == PROJECT_ID
        assert model_name == "qwen-small-prod"
        assert tail == "25"
        return expected

    monkeypatch.setattr(
        "app.routes.model_deployments.model_deployment_service.list_model_logs",
        list_model_logs,
    )

    response = client.get(
        f"/v1/projects/{PROJECT_ID}/models/qwen-small-prod/logs?tail=25",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == expected


def test_list_model_logs_service(monkeypatch) -> None:
    fake = FakeTransaction(fetchones=[{"role": "viewer"}, deployment_row_fixture()])
    monkeypatch.setattr(model_deployment_service, "transaction", fake.transaction)
    monkeypatch.setattr(
        model_deployment_service.k8s_client,
        "create_clients",
        lambda: "clients",
    )
    monkeypatch.setattr(
        model_deployment_service.deployment_manager,
        "read_model_logs",
        lambda clients, deployment, tail_lines: [{"pod": "pod-a", "text": "ok"}],
    )

    response = model_deployment_service.list_model_logs(
        USER_ID,
        PROJECT_ID,
        "qwen-small-prod",
        tail="25",
    )

    assert response["logs"] == [{"pod": "pod-a", "text": "ok"}]
    assert fake.cursor.executed[-1][1]["name"] == "qwen-small-prod"


def test_get_model_deployment_status_service(monkeypatch) -> None:
    deployment = deployment_row_fixture()
    job = deployment_job_row_fixture()
    fake = FakeTransaction(fetchones=[{"role": "viewer"}, deployment])
    fake.cursor.fetchall_rows = [job]
    monkeypatch.setattr(model_deployment_service, "transaction", fake.transaction)
    monkeypatch.setattr(
        model_deployment_service,
        "inspect_kubernetes_status",
        lambda deployment: {"available": False, "reason": "cluster unavailable"},
    )

    response = get_model_deployment_status(USER_ID, PROJECT_ID, MODEL_DEPLOYMENT_ID)

    assert response["modelDeployment"]["modelDeploymentID"] == MODEL_DEPLOYMENT_ID
    assert response["latestDeploymentJob"]["deploymentJobID"] == DEPLOYMENT_JOB_ID
    assert response["kubernetes"]["reason"] == "cluster unavailable"


def test_inspect_kubernetes_status_reports_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        model_deployment_service.k8s_client,
        "create_clients",
        lambda: (_ for _ in ()).throw(RuntimeError("cluster offline")),
    )

    response = model_deployment_service.inspect_kubernetes_status(deployment_row_fixture())

    assert response["available"] is False
    assert response["reason"] == "cluster offline"


def test_parse_log_tail_rejects_bad_values() -> None:
    with pytest.raises(ApiError):
        model_deployment_service.parse_log_tail("1001")


@pytest.mark.parametrize(
    ("path_suffix", "method_name", "job_type"),
    [
        ("start", "start_model_deployment", "start_model"),
        ("stop", "stop_model_deployment", "stop_model"),
    ],
)
def test_lifecycle_command_routes(
    monkeypatch,
    client,
    auth_headers,
    path_suffix,
    method_name,
    job_type,
) -> None:
    bypass_idempotency(monkeypatch)

    monkeypatch.setattr(
        f"app.routes.model_deployments.model_deployment_service.{method_name}",
        lambda user_id, project_id, model_deployment_id: command_response(job_type),
    )

    response = client.post(
        f"/v1/projects/{PROJECT_ID}/models/{MODEL_DEPLOYMENT_ID}/{path_suffix}",
        headers=idempotency_headers(auth_headers, path_suffix),
    )

    assert response.status_code == 202
    assert response.get_json() == command_response(job_type)


def test_scale_model_deployment_route(monkeypatch, client, auth_headers) -> None:
    bypass_idempotency(monkeypatch)

    def scale_model_deployment(user_id, project_id, model_deployment_id, replicas):
        assert replicas == 3
        return command_response("scale_model")

    monkeypatch.setattr(
        "app.routes.model_deployments.model_deployment_service."
        "scale_model_deployment",
        scale_model_deployment,
    )

    response = client.post(
        f"/v1/projects/{PROJECT_ID}/models/{MODEL_DEPLOYMENT_ID}/scale",
        json={"replicas": 3},
        headers=idempotency_headers(auth_headers, "scale-model"),
    )

    assert response.status_code == 202
    assert response.get_json() == command_response("scale_model")


def test_update_model_deployment_settings_route(monkeypatch, client, auth_headers) -> None:
    bypass_idempotency(monkeypatch)

    def update_settings(user_id, project_id, model_deployment_id, data):
        assert data["resources"]["gpu_count"] == 1
        return command_response("update_model")

    monkeypatch.setattr(
        "app.routes.model_deployments.model_deployment_service."
        "update_model_deployment_settings",
        update_settings,
    )

    response = client.patch(
        f"/v1/projects/{PROJECT_ID}/models/{MODEL_DEPLOYMENT_ID}",
        json={"resources": {"gpu_count": 1}},
        headers=idempotency_headers(auth_headers, "update-model"),
    )

    assert response.status_code == 202
    assert response.get_json() == command_response("update_model")


def test_sync_model_deployment_status_route(monkeypatch, client, auth_headers) -> None:
    bypass_idempotency(monkeypatch)
    monkeypatch.setattr(
        "app.routes.model_deployments.model_deployment_service."
        "sync_model_deployment_status",
        lambda user_id, project_id, model_deployment_id: command_response("sync_status"),
    )

    response = client.post(
        f"/v1/projects/{PROJECT_ID}/models/{MODEL_DEPLOYMENT_ID}/sync",
        headers=idempotency_headers(auth_headers, "sync-model"),
    )

    assert response.status_code == 202
    assert response.get_json() == command_response("sync_status")


def test_delete_model_deployment_route(monkeypatch, client, auth_headers) -> None:
    bypass_idempotency(monkeypatch)

    monkeypatch.setattr(
        "app.routes.model_deployments.model_deployment_service."
        "delete_model_deployment",
        lambda user_id, project_id, model_deployment_id: command_response("delete_model"),
    )

    response = client.delete(
        f"/v1/projects/{PROJECT_ID}/models/{MODEL_DEPLOYMENT_ID}",
        headers=idempotency_headers(auth_headers, "delete-model"),
    )

    assert response.status_code == 202
    assert response.get_json() == command_response("delete_model")


def test_model_deployment_routes_require_auth(client) -> None:
    response = client.get(f"/v1/projects/{PROJECT_ID}/models")

    assert response.status_code == 401
    assert response.get_json()["error"]["type"] == "unauthorized"


def test_model_deployment_command_routes_require_idempotency_key(
    client,
    auth_headers,
) -> None:
    response = client.post(
        f"/v1/projects/{PROJECT_ID}/models",
        json={"name": "qwen-small-prod", "model_id": "Qwen/Qwen2.5-0.5B-Instruct"},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["type"] == "missing_idempotency_key"


def test_validate_deployment_spec_applies_defaults(app) -> None:
    with app.app_context():
        spec = validate_deployment_spec(
            {
                "name": "qwen-small-prod",
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
            }
        )

    assert spec["replicas"] == 1
    assert spec["cpu_request"] == "1"
    assert spec["memory_limit"] == "8Gi"
    assert spec["vllm_image"] == "vllm/vllm-openai-cpu:latest-x86_64"
    assert spec["autoscaling_enabled"] is False
    assert spec["min_replicas"] is None


def test_validate_deployment_spec_uses_gpu_image_when_gpu_requested(app) -> None:
    with app.app_context():
        spec = validate_deployment_spec(
            {
                "name": "qwen-small-prod",
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                "resources": {"gpu_count": 1},
            }
        )

    assert spec["gpu_count"] == 1
    assert spec["vllm_image"] == "vllm/vllm-openai:latest"


def test_validate_deployment_spec_rejects_client_vllm_image(app) -> None:
    with app.app_context(), pytest.raises(ApiError) as error:
        validate_deployment_spec(
            {
                "name": "qwen-small-prod",
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                "vllm": {"image": "example/custom:latest"},
            }
        )

    assert error.value.type == "validation_error"
    assert "managed by MiniTen" in error.value.message


def test_validate_deployment_spec_uses_debug_smoke_image(app) -> None:
    with app.app_context():
        spec = validate_deployment_spec(
            {
                "name": "smoke",
                "model_id": "miniten/smoke-openai-compatible",
            }
        )

    assert spec["vllm_image"] == "python:3.12-alpine"


def test_validate_deployment_spec_rejects_bad_autoscaling(app) -> None:
    with app.app_context(), pytest.raises(ApiError) as error:
        validate_deployment_spec(
            {
                "name": "qwen-small-prod",
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                "autoscaling": {
                    "enabled": True,
                    "min_replicas": 4,
                    "max_replicas": 2,
                },
            }
        )

    assert error.value.type == "validation_error"


def test_validate_deployment_spec_rejects_unknown_fields(app) -> None:
    with app.app_context(), pytest.raises(ApiError) as error:
        validate_deployment_spec(
            {
                "name": "qwen-small-prod",
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                "resources": {"bad": "field"},
            }
        )

    assert error.value.type == "validation_error"
    assert error.value.details["unsupported_fields"] == ["bad"]


def test_validate_deployment_spec_rejects_bad_resource_quantities(app) -> None:
    with app.app_context(), pytest.raises(ApiError) as error:
        validate_deployment_spec(
            {
                "name": "qwen-small-prod",
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                "resources": {"cpu_request": "fast"},
            }
        )

    assert "CPU quantity" in error.value.message


def test_validate_deployment_spec_rejects_bad_dtype(app) -> None:
    with app.app_context(), pytest.raises(ApiError) as error:
        validate_deployment_spec(
            {
                "name": "qwen-small-prod",
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                "vllm": {"dtype": "int2"},
            }
        )

    assert error.value.details["field"] == "dtype"


def test_validate_deployment_spec_rejects_replicas_outside_autoscaling(app) -> None:
    with app.app_context(), pytest.raises(ApiError) as error:
        validate_deployment_spec(
            {
                "name": "qwen-small-prod",
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                "replicas": 1,
                "autoscaling": {
                    "enabled": True,
                    "min_replicas": 2,
                    "max_replicas": 4,
                },
            }
        )

    assert "min_replicas" in error.value.message


def test_validate_deployment_update_reselects_gpu_image(app) -> None:
    current = deployment_row_fixture()
    with app.app_context():
        spec = model_deployment_service.validate_deployment_update(
            {"resources": {"gpu_count": 1}},
            current,
        )

    assert spec["name"] == current["name"]
    assert spec["gpu_count"] == 1
    assert spec["vllm_image"] == "vllm/vllm-openai:latest"


def test_validate_deployment_update_rejects_identity_changes(app) -> None:
    with app.app_context(), pytest.raises(ApiError):
        model_deployment_service.validate_deployment_update(
            {"model_id": "other/model"},
            deployment_row_fixture(),
        )


def test_build_k8s_names() -> None:
    assert build_k8s_names("miniten-personal", "qwen-small-prod") == {
        "k8s_namespace": "miniten-personal",
        "k8s_deployment_name": "qwen-small-prod-v1",
        "k8s_service_name": "qwen-small-prod",
        "k8s_hpa_name": "qwen-small-prod-v1",
    }


def test_serialize_model_deployment_and_job() -> None:
    deployment_row = deployment_row_fixture()
    job_row = {
        "deployment_job_id": DEPLOYMENT_JOB_ID,
        "project_id": PROJECT_ID,
            "model_deployment_id": MODEL_DEPLOYMENT_ID,
            "job_type": "deploy_model",
            "desired_generation": 1,
            "status": "queued",
        "attempts": 0,
        "max_attempts": 3,
        "last_error": None,
        "created_at": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
    }

    assert serialize_model_deployment(deployment_row) == model_deployment_response()
    assert serialize_deployment_job(job_row) == deployment_job_response()


def test_build_job_payload() -> None:
    payload = build_job_payload(
        "scale_model",
        deployment_row_fixture(),
        {"replicas": 3},
    )

    assert payload["job_type"] == "scale_model"
    assert payload["model_deployment_id"] == MODEL_DEPLOYMENT_ID
    assert payload["replicas"] == 3
    assert payload["k8s_service_name"] == "qwen-small-prod"


def test_model_deployment_not_found_error() -> None:
    error = model_deployment_not_found_error()

    assert error.type == "model_deployment_not_found"
    assert error.status_code == 404


def deployment_row_fixture() -> dict[str, object]:
    return {
        "model_deployment_id": MODEL_DEPLOYMENT_ID,
        "project_id": PROJECT_ID,
        "name": "qwen-small-prod",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "status": "deploying",
        "k8s_namespace": "miniten-personal",
        "k8s_deployment_name": "qwen-small-prod-v1",
        "k8s_service_name": "qwen-small-prod",
        "k8s_hpa_name": "qwen-small-prod-v1",
        "replicas": 1,
        "desired_generation": 1,
        "cpu_request": "2",
        "cpu_limit": "4",
        "memory_request": "8Gi",
        "memory_limit": "16Gi",
        "gpu_count": 0,
        "vllm_image": "vllm/vllm-openai:latest",
        "vllm_dtype": "auto",
        "vllm_max_model_len": 4096,
        "autoscaling_enabled": True,
        "min_replicas": 1,
        "max_replicas": 3,
        "target_cpu_utilization": 70,
        "created_at": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
        "deleted_at": None,
    }


def deployment_job_row_fixture() -> dict[str, object]:
    return {
        "deployment_job_id": DEPLOYMENT_JOB_ID,
        "project_id": PROJECT_ID,
        "model_deployment_id": MODEL_DEPLOYMENT_ID,
        "job_type": "deploy_model",
        "desired_generation": 1,
        "status": "queued",
        "attempts": 0,
        "max_attempts": 3,
        "last_error": None,
        "created_at": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
    }


class FakeCursor:
    def __init__(self, *, fetchones=None):
        self.fetchones = list(fetchones or [])
        self.fetchall_rows = []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.fetchones.pop(0) if self.fetchones else None

    def fetchall(self):
        return self.fetchall_rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    @contextmanager
    def cursor(self):
        yield self._cursor


class FakeTransaction:
    def __init__(self, *, fetchones=None):
        self.cursor = FakeCursor(fetchones=fetchones)

    @contextmanager
    def transaction(self):
        yield FakeConnection(self.cursor)
