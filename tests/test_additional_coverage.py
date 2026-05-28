"""Additional edge-case coverage across implemented MiniTen modules."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from flask import Flask, g

from app import create_app
from app.k8s import client as k8s_client
from app.k8s.manifests import (
    build_model_manifests,
    build_resource_requirements,
    common_labels,
    model_labels,
    model_selector_labels,
)
from app.k8s.names import append_suffix
from app.security import api_keys
from app.security.tokens import (
    ALGORITHM,
    TOKEN_TYPE,
    create_access_token,
    current_user_id,
    decode_access_token,
    get_bearer_token,
    unauthorized_error,
)
from app.services import auth_service, deployment_worker, project_service, user_service
from app.services.model_deployment_service import validate_deployment_spec
from app.utils.errors import ApiError, ValidationError, error_response
from app.utils.time import to_iso8601, utc_now_plus
from app.utils.validation import (
    require_field,
    validate_api_key_name,
    validate_positive_int,
    validate_string,
)


class TestConfig:
    TESTING = True
    API_DEBUG = True
    SECRET_KEY = "test-secret-key-change-me-32-bytes"
    API_KEY_HASH_SECRET = "test-api-key-hash-secret-change-me-32-bytes"
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


def deployment_payload(**overrides):
    payload = {
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
        "autoscaling_enabled": False,
        "min_replicas": None,
        "max_replicas": None,
        "target_cpu_utilization": None,
    }
    payload.update(overrides)
    return payload


def deployment_row(**overrides):
    row = {
        "model_deployment_id": "bf3dc090-5bb4-46f6-964d-6cd8375ddf56",
        "project_id": "a2fc41b7-862e-4060-b466-2376f29227bb",
        **deployment_payload(),
    }
    row.update(overrides)
    return row


def job_row(job_type="deploy_model", **overrides):
    row = {
        "deployment_job_id": "3ef7d993-cb61-4392-b36b-2ed2e1d88af1",
        "project_id": "a2fc41b7-862e-4060-b466-2376f29227bb",
        "model_deployment_id": "bf3dc090-5bb4-46f6-964d-6cd8375ddf56",
        "job_type": job_type,
        "attempts": 0,
        "max_attempts": 3,
    }
    row.update(overrides)
    return row


def test_api_error_includes_details() -> None:
    error = ApiError("validation_error", "Bad input.", 400, {"field": "name"})

    assert error.to_dict()["error"]["details"] == {"field": "name"}


def test_error_response_builds_json_tuple() -> None:
    app = Flask(__name__)

    with app.app_context():
        response, status = error_response(
            "conflict",
            "Already exists.",
            409,
            details={"field": "email"},
        )

    assert status == 409
    assert response.get_json()["error"]["details"] == {"field": "email"}


def test_validation_error_defaults() -> None:
    error = ValidationError()

    assert error.type == "validation_error"
    assert error.status_code == 400
    assert error.message == "Invalid request."


def test_require_field_returns_present_falsey_values() -> None:
    assert require_field({"replicas": 0}, "replicas") == 0
    assert require_field({"enabled": False}, "enabled") is False


def test_require_field_rejects_missing_none() -> None:
    with pytest.raises(ValidationError) as error:
        require_field({"name": None}, "name")

    assert error.value.details == {"field": "name"}


def test_validate_string_trims_and_rejects_too_long() -> None:
    assert validate_string("  local-dev  ", "name", max_length=20) == "local-dev"

    with pytest.raises(ValidationError):
        validate_string("x" * 21, "name", max_length=20)


def test_validate_api_key_name_bounds() -> None:
    assert validate_api_key_name(" Production ") == "Production"

    with pytest.raises(ValidationError):
        validate_api_key_name("x" * 81)


def test_validate_positive_int_rejects_zero_by_default() -> None:
    with pytest.raises(ValidationError):
        validate_positive_int(0, "replicas")


def test_validate_positive_int_allows_zero_when_min_zero() -> None:
    assert validate_positive_int(0, "replicas", min_value=0) == 0


def test_to_iso8601_treats_naive_datetime_as_utc() -> None:
    assert to_iso8601(datetime(2026, 5, 17, 12, 0)) == "2026-05-17T12:00:00Z"


def test_utc_now_plus_returns_future_datetime() -> None:
    before = datetime.now(UTC)
    future = utc_now_plus(seconds=10)

    assert future > before


def test_get_bearer_token_accepts_case_insensitive_scheme(app) -> None:
    with app.test_request_context(headers={"Authorization": "bEaReR token-value"}):
        assert get_bearer_token() == "token-value"


@pytest.mark.parametrize(
    "authorization",
    ["", "Basic abc", "Bearer", "Bearer "],
)
def test_get_bearer_token_rejects_missing_or_bad_header(app, authorization) -> None:
    with app.test_request_context(headers={"Authorization": authorization}):
        with pytest.raises(ApiError) as error:
            get_bearer_token()

    assert error.value.type == "unauthorized"


def test_current_user_id_requires_authenticated_context(app) -> None:
    with app.test_request_context():
        with pytest.raises(ApiError):
            current_user_id()


def test_current_user_id_returns_g_value(app) -> None:
    with app.test_request_context():
        g.current_user_id = "user-123"

        assert current_user_id() == "user-123"


def test_decode_access_token_rejects_wrong_type(app) -> None:
    now = datetime.now(UTC)
    with app.app_context():
        token = jwt.encode(
            {
                "sub": "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e",
                "type": "project_api_key",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            app.config["SECRET_KEY"],
            algorithm=ALGORITHM,
        )

        with pytest.raises(ApiError):
            decode_access_token(token)


def test_decode_access_token_rejects_missing_subject(app) -> None:
    now = datetime.now(UTC)
    with app.app_context():
        token = jwt.encode(
            {"type": TOKEN_TYPE, "iat": now, "exp": now + timedelta(minutes=5)},
            app.config["SECRET_KEY"],
            algorithm=ALGORITHM,
        )

        with pytest.raises(ApiError):
            decode_access_token(token)


def test_decode_access_token_rejects_expired_token(app) -> None:
    with app.app_context():
        token = create_access_token(
            "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e",
            expires_delta=timedelta(seconds=-1),
        )

        with pytest.raises(ApiError):
            decode_access_token(token)


def test_unauthorized_error_shape() -> None:
    error = unauthorized_error()

    assert error.type == "unauthorized"
    assert error.status_code == 401


def test_api_key_hash_changes_with_server_secret(app) -> None:
    with app.app_context():
        raw_key, _ = api_keys.generate_api_key()

    first = api_keys.hash_api_key(raw_key, "first-secret")
    second = api_keys.hash_api_key(raw_key, "second-secret")

    assert first != second


@pytest.mark.parametrize(
    "raw_key",
    [
        "mt_live_onlytwo",
        "wrong_live_visible_secret",
        "mt_test_visible_secret",
        "mt_live__secret",
        "mt_live_visible_",
    ],
)
def test_derive_key_prefix_rejects_malformed_keys(raw_key) -> None:
    with pytest.raises(ApiError):
        api_keys.derive_key_prefix(raw_key)


def test_auth_service_logout_response() -> None:
    assert auth_service.logout() == {"logged_out": True}


def test_invalid_credentials_error_is_generic() -> None:
    error = auth_service.invalid_credentials_error()

    assert error.type == "invalid_credentials"
    assert "email or password" in error.message


def test_user_serializer_includes_last_login_when_present() -> None:
    row = {
        "user_id": "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e",
        "email": "user@example.com",
        "created_at": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
        "last_login_at": datetime(2026, 5, 17, 12, 5, tzinfo=UTC),
    }

    response = user_service.serialize_user(row)

    assert response["last_login_at"] == "2026-05-17T12:05:00Z"


def test_project_slug_suffix_preserves_dns_limit() -> None:
    base = "a" * 63

    value = project_service._join_slug_suffix(base, "-99")

    assert value.endswith("-99")
    assert len(value) <= 63


def test_project_slug_suffixes_start_with_unsuffixed_name() -> None:
    suffixes = project_service._slug_suffixes()

    assert suffixes[:3] == ["", "-2", "-3"]


def test_project_not_found_error_is_access_boundary_safe() -> None:
    error = project_service.project_not_found_error()

    assert error.type == "project_not_found"
    assert error.status_code == 404


def test_validate_deployment_spec_rejects_non_object_resources(app) -> None:
    with app.app_context(), pytest.raises(ApiError) as error:
        validate_deployment_spec(
            {
                "name": "qwen-small-prod",
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                "resources": [],
            }
        )

    assert "resources" in error.value.message


def test_validate_deployment_spec_rejects_boolean_gpu_count(app) -> None:
    with app.app_context(), pytest.raises(ValidationError):
        validate_deployment_spec(
            {
                "name": "qwen-small-prod",
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                "resources": {"gpu_count": True},
            }
        )


def test_validate_deployment_spec_applies_autoscaling_defaults(app) -> None:
    with app.app_context():
        spec = validate_deployment_spec(
            {
                "name": "qwen-small-prod",
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                "autoscaling": {"enabled": True},
            }
        )

    assert spec["autoscaling_enabled"] is True
    assert spec["min_replicas"] == 1
    assert spec["max_replicas"] == 3
    assert spec["target_cpu_utilization"] == 70


def test_append_suffix_rejects_overlong_suffix() -> None:
    with pytest.raises(ValueError):
        append_suffix("valid-name", "x" * 64)


def test_manifest_labels_are_stable() -> None:
    assert common_labels("miniten-personal")["app.kubernetes.io/name"] == "miniten"
    assert model_selector_labels("qwen-small-prod") == {
        "app.kubernetes.io/name": "miniten",
        "miniten.io/model": "qwen-small-prod",
    }
    assert model_labels("miniten-personal", "qwen-small-prod")[
        "miniten.io/project-namespace"
    ] == "miniten-personal"


def test_build_resource_requirements_omits_none_values() -> None:
    resources = build_resource_requirements(
        deployment_payload(cpu_request=None, memory_limit=None)
    )

    assert "cpu" not in resources["requests"]
    assert "memory" not in resources["limits"]


def test_build_model_manifests_omits_optional_secret_and_hpa() -> None:
    manifests = build_model_manifests(deployment_payload())

    assert manifests["secret"] is None
    assert manifests["hpa"] is None


def test_client_apply_service_patches_on_conflict() -> None:
    core = FakeCore(conflict_on_create=True)
    clients = k8s_client.KubernetesClients(core=core, apps=FakeApps(), autoscaling=FakeHpa())
    manifest = {"metadata": {"name": "svc", "namespace": "ns"}}

    k8s_client.apply_service(clients, manifest)

    assert core.calls == ["create_namespaced_service", "patch_namespaced_service"]


def test_client_apply_deployment_patches_on_conflict() -> None:
    apps = FakeApps(conflict_on_create=True)
    clients = k8s_client.KubernetesClients(core=FakeCore(), apps=apps, autoscaling=FakeHpa())
    manifest = {"metadata": {"name": "deploy", "namespace": "ns"}}

    k8s_client.apply_deployment(clients, manifest)

    assert apps.calls == ["create_namespaced_deployment", "patch_namespaced_deployment"]


def test_client_delete_deployment_reraises_non_404() -> None:
    apps = FakeApps(delete_status=500)
    clients = k8s_client.KubernetesClients(core=FakeCore(), apps=apps, autoscaling=FakeHpa())

    with pytest.raises(FakeApiException):
        k8s_client.delete_deployment(clients, "ns", "deploy")


def test_worker_default_worker_id_contains_host_and_pid() -> None:
    worker_id = deployment_worker.default_worker_id()

    assert ":" in worker_id


def test_worker_dispatch_rejects_unknown_job_type() -> None:
    with pytest.raises(RuntimeError):
        deployment_worker.dispatch_job(
            object(),
            job_row("unknown_job"),
            deployment_row(),
        )


def test_worker_mark_success_for_running_status(monkeypatch) -> None:
    fake = FakeTransaction()
    events = []

    monkeypatch.setattr(deployment_worker, "transaction", fake.transaction)
    monkeypatch.setattr(
        deployment_worker,
        "update_deployment_status_with_cursor",
        lambda cur, model_deployment_id, status: deployment_row(status=status),
    )
    monkeypatch.setattr(
        deployment_worker,
        "create_model_event_with_cursor",
        lambda cur, deployment, event_type, message, metadata: events.append(event_type),
    )

    deployment_worker.mark_job_succeeded(job_row("deploy_model"), deployment_row())

    assert events == ["model_running"]
    assert fake.cursor.executed[-1]["deployment_job_id"] == job_row()["deployment_job_id"]


def test_worker_mark_success_for_delete_marks_deleted(monkeypatch) -> None:
    fake = FakeTransaction(fetchone=deployment_row(status="deleted"))
    events = []

    monkeypatch.setattr(deployment_worker, "transaction", fake.transaction)
    monkeypatch.setattr(
        deployment_worker,
        "create_model_event_with_cursor",
        lambda cur, deployment, event_type, message, metadata: events.append(event_type),
    )

    deployment_worker.mark_job_succeeded(job_row("delete_model"), deployment_row())

    assert events == ["model_deleted"]
    assert len(fake.cursor.executed) == 2


def test_worker_failure_marks_retrying(monkeypatch) -> None:
    fake = FakeTransaction(fetchone=deployment_row(status="failed"))
    events = []

    monkeypatch.setattr(deployment_worker, "transaction", fake.transaction)
    monkeypatch.setattr(
        deployment_worker,
        "update_deployment_status_with_cursor",
        lambda cur, model_deployment_id, status: deployment_row(status=status),
    )
    monkeypatch.setattr(
        deployment_worker,
        "create_model_event_with_cursor",
        lambda cur, deployment, event_type, message, metadata: events.append(metadata),
    )

    deployment_worker.mark_job_failed_or_retrying(
        job_row(attempts=0, max_attempts=2),
        RuntimeError("temporary"),
    )

    assert events[0]["will_retry"] is True
    assert fake.cursor.executed[-1]["last_error"] == "unknown: temporary"


def test_worker_failure_marks_permanent_failed(monkeypatch) -> None:
    fake = FakeTransaction(fetchone=deployment_row(status="failed"))
    events = []

    monkeypatch.setattr(deployment_worker, "transaction", fake.transaction)
    monkeypatch.setattr(
        deployment_worker,
        "update_deployment_status_with_cursor",
        lambda cur, model_deployment_id, status: deployment_row(status=status),
    )
    monkeypatch.setattr(
        deployment_worker,
        "create_model_event_with_cursor",
        lambda cur, deployment, event_type, message, metadata: events.append(metadata),
    )

    deployment_worker.mark_job_failed_or_retrying(
        job_row(attempts=1, max_attempts=2),
        RuntimeError("permanent"),
    )

    assert events[0]["will_retry"] is False
    assert fake.cursor.executed[-1]["last_error"] == "unknown: permanent"


class FakeApiException(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


class FakeCore:
    def __init__(self, *, conflict_on_create=False):
        self.conflict_on_create = conflict_on_create
        self.calls = []

    def create_namespaced_service(self, namespace, manifest):
        self.calls.append("create_namespaced_service")
        if self.conflict_on_create:
            raise FakeApiException(409)
        return manifest

    def patch_namespaced_service(self, name, namespace, manifest):
        self.calls.append("patch_namespaced_service")
        return manifest


class FakeApps:
    def __init__(self, *, conflict_on_create=False, delete_status=None):
        self.conflict_on_create = conflict_on_create
        self.delete_status = delete_status
        self.calls = []

    def create_namespaced_deployment(self, namespace, manifest):
        self.calls.append("create_namespaced_deployment")
        if self.conflict_on_create:
            raise FakeApiException(409)
        return manifest

    def patch_namespaced_deployment(self, name, namespace, manifest):
        self.calls.append("patch_namespaced_deployment")
        return manifest

    def delete_namespaced_deployment(self, name, namespace):
        self.calls.append("delete_namespaced_deployment")
        if self.delete_status is not None:
            raise FakeApiException(self.delete_status)
        return None


class FakeHpa:
    pass


class FakeCursor:
    def __init__(self, fetchone=None):
        self._fetchone = fetchone
        self.executed = []

    def execute(self, sql, params):
        self.executed.append(params)

    def fetchone(self):
        return self._fetchone


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    @contextmanager
    def cursor(self):
        yield self._cursor


class FakeTransaction:
    def __init__(self, fetchone=None):
        self.cursor = FakeCursor(fetchone)

    @contextmanager
    def transaction(self):
        yield FakeConnection(self.cursor)
