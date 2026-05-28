"""Service-level database path tests with fake cursors.

Route tests prove Flask wiring. These tests exercise the actual service
functions by replacing database transactions with deterministic fake
connections/cursors, so the business logic is covered without a live Postgres.
"""

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from app import create_app
from app.services import (
    api_key_service,
    auth_service,
    model_deployment_service,
    project_service,
    user_service,
)
from app.utils.errors import ApiError


PROJECT_ID = "a2fc41b7-862e-4060-b466-2376f29227bb"
USER_ID = "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e"
TARGET_USER_ID = "5bcb59e9-86e8-4ac9-b5c6-66f332697a0c"
MODEL_DEPLOYMENT_ID = "bf3dc090-5bb4-46f6-964d-6cd8375ddf56"
API_KEY_ID = "4c659341-7357-42f6-9ee8-801a1f340b35"
JOB_ID = "3ef7d993-cb61-4392-b36b-2ed2e1d88af1"
NOW = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)


class TestConfig:
    TESTING = True
    SECRET_KEY = "test-secret-key-change-me-32-bytes"
    API_KEY_HASH_SECRET = "test-api-key-hash-secret-change-me-32-bytes"
    DEFAULT_MODEL_REPLICAS = 1
    DEFAULT_HPA_MIN_REPLICAS = 1
    DEFAULT_HPA_MAX_REPLICAS = 3
    DEFAULT_HPA_TARGET_CPU_UTILIZATION = 70
    VLLM_IMAGE = "vllm/vllm-openai:latest"


@pytest.fixture
def app():
    return create_app(TestConfig)


def user_row(**overrides):
    row = {
        "user_id": USER_ID,
        "email": "user@example.com",
        "hashed_password": "hashed",
        "created_at": NOW,
        "last_login_at": None,
    }
    row.update(overrides)
    return row


def project_row(**overrides):
    row = {
        "project_id": PROJECT_ID,
        "name": "Personal Models",
        "slug": "personal-models",
        "k8s_namespace": "miniten-personal-models",
        "created_at": NOW,
        "role": "owner",
    }
    row.update(overrides)
    return row


def member_row(**overrides):
    row = {
        "user_id": TARGET_USER_ID,
        "email": "member@example.com",
        "role": "member",
        "created_at": NOW,
    }
    row.update(overrides)
    return row


def deployment_row(**overrides):
    row = {
        "model_deployment_id": MODEL_DEPLOYMENT_ID,
        "project_id": PROJECT_ID,
        "name": "qwen-small-prod",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "status": "deploying",
        "k8s_namespace": "miniten-personal-models",
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
        "created_at": NOW,
        "updated_at": NOW,
        "deleted_at": None,
    }
    row.update(overrides)
    return row


def job_row(**overrides):
    row = {
        "deployment_job_id": JOB_ID,
        "project_id": PROJECT_ID,
        "model_deployment_id": MODEL_DEPLOYMENT_ID,
        "job_type": "deploy_model",
        "status": "queued",
        "attempts": 0,
        "max_attempts": 3,
        "last_error": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def api_key_row(**overrides):
    row = {
        "api_key_id": API_KEY_ID,
        "project_id": PROJECT_ID,
        "name": "Production",
        "key_prefix": "mt_live_visible",
        "key_hash": "sha256:hash",
        "created_at": NOW,
        "last_used_at": None,
        "revoked_at": None,
    }
    row.update(overrides)
    return row


def test_user_service_create_get_delete(monkeypatch) -> None:
    fake = FakeTransaction(fetchones=[user_row(), user_row(), {"user_id": USER_ID}])
    monkeypatch.setattr(user_service, "transaction", fake.transaction)
    monkeypatch.setattr(user_service, "hash_password", lambda password: "hashed-password")

    created = user_service.create_user(" USER@EXAMPLE.COM ", "password123")
    fetched = user_service.get_user(USER_ID)
    deleted = user_service.delete_user(USER_ID)

    assert created["email"] == "user@example.com"
    assert fetched["userID"] == USER_ID
    assert deleted == {"deleted": True}
    assert fake.cursor.executed[0][1]["hashed_password"] == "hashed-password"


def test_user_service_not_found_errors(monkeypatch) -> None:
    fake = FakeTransaction(fetchones=[None, None])
    monkeypatch.setattr(user_service, "transaction", fake.transaction)

    with pytest.raises(ApiError):
        user_service.get_user(USER_ID)

    with pytest.raises(ApiError):
        user_service.delete_user(USER_ID)


def test_user_service_unique_violation(monkeypatch) -> None:
    fake = FakeTransaction(execute_errors=[UniqueViolation()])
    monkeypatch.setattr(user_service, "transaction", fake.transaction)
    monkeypatch.setattr(user_service, "hash_password", lambda password: "hashed-password")

    with pytest.raises(ApiError) as error:
        user_service.create_user("user@example.com", "password123")

    assert error.value.type == "email_already_exists"


def test_auth_service_login_success(monkeypatch, app) -> None:
    fake = FakeTransaction(fetchones=[user_row(), user_row(last_login_at=NOW)])
    monkeypatch.setattr(auth_service, "transaction", fake.transaction)
    monkeypatch.setattr(auth_service, "verify_password", lambda password, hashed: True)

    with app.app_context():
        response = auth_service.login("USER@EXAMPLE.COM", "password123")

    assert response["token_type"] == "bearer"
    assert response["user"]["last_login_at"] == "2026-05-17T12:00:00Z"


def test_auth_service_login_rejects_missing_user_or_bad_password(monkeypatch) -> None:
    fake = FakeTransaction(fetchones=[None, user_row()])
    monkeypatch.setattr(auth_service, "transaction", fake.transaction)
    monkeypatch.setattr(auth_service, "verify_password", lambda password, hashed: False)

    with pytest.raises(ApiError):
        auth_service.login("missing@example.com", "password123")

    with pytest.raises(ApiError):
        auth_service.login("user@example.com", "password123")


def test_project_service_create_list_get_delete(monkeypatch) -> None:
    fake = FakeTransaction(
        fetchones=[
            project_row(),
            project_row(role="owner"),
            {"role": "owner"},
            {"project_id": PROJECT_ID},
        ],
        fetchalls=[[project_row(role="owner")]],
    )
    monkeypatch.setattr(project_service, "transaction", fake.transaction)

    created = project_service.create_project(USER_ID, "Personal Models")
    listed = project_service.list_projects(USER_ID)
    fetched = project_service.get_project(USER_ID, PROJECT_ID)
    deleted = project_service.delete_project(USER_ID, PROJECT_ID)

    assert created["slug"] == "personal-models"
    assert listed["projects"][0]["projectID"] == PROJECT_ID
    assert fetched["role"] == "owner"
    assert deleted == {"deleted": True}


def test_project_service_member_lifecycle(monkeypatch) -> None:
    fake = FakeTransaction(
        fetchones=[
            {"role": "owner"},
            {"role": "owner"},
            user_row(user_id=TARGET_USER_ID, email="member@example.com"),
            {"role": "member", "created_at": NOW},
            {"role": "owner"},
            member_row(role="member"),
            {"role": "viewer", "created_at": NOW},
            {"role": "owner"},
            member_row(role="viewer"),
        ],
        fetchalls=[[member_row()]],
    )
    monkeypatch.setattr(project_service, "transaction", fake.transaction)

    members = project_service.list_project_members(USER_ID, PROJECT_ID)
    added = project_service.add_project_member(
        USER_ID,
        PROJECT_ID,
        "member@example.com",
        "member",
    )
    updated = project_service.update_project_member_role(
        USER_ID,
        PROJECT_ID,
        TARGET_USER_ID,
        "viewer",
    )
    removed = project_service.remove_project_member(USER_ID, PROJECT_ID, TARGET_USER_ID)

    assert members["members"][0]["email"] == "member@example.com"
    assert added["role"] == "member"
    assert updated["role"] == "viewer"
    assert removed == {"removed": True}


def test_project_service_protects_last_owner(monkeypatch) -> None:
    fake = FakeTransaction(fetchones=[{"owner_count": 1}])
    monkeypatch.setattr(project_service, "transaction", fake.transaction)

    with pytest.raises(ApiError):
        project_service.ensure_not_last_owner(fake.cursor, PROJECT_ID)


def test_project_service_validation_and_not_found_branches(monkeypatch) -> None:
    with pytest.raises(ApiError):
        project_service.create_project(USER_ID, "!!!")

    fake = FakeTransaction(fetchones=[None, {"role": "owner"}, None])
    monkeypatch.setattr(project_service, "transaction", fake.transaction)

    with pytest.raises(ApiError):
        project_service.get_project(USER_ID, PROJECT_ID)

    with pytest.raises(ApiError):
        project_service.delete_project(USER_ID, PROJECT_ID)


def test_project_service_member_error_branches(monkeypatch) -> None:
    fake = FakeTransaction(
        fetchones=[
            {"role": "owner"},
            None,
            {"role": "owner"},
            None,
            {"role": "owner"},
            None,
        ],
    )
    monkeypatch.setattr(project_service, "transaction", fake.transaction)

    with pytest.raises(ApiError):
        project_service.add_project_member(
            USER_ID,
            PROJECT_ID,
            "missing@example.com",
            "member",
        )

    with pytest.raises(ApiError):
        project_service.update_project_member_role(
            USER_ID,
            PROJECT_ID,
            TARGET_USER_ID,
            "viewer",
        )

    with pytest.raises(ApiError):
        project_service.remove_project_member(USER_ID, PROJECT_ID, TARGET_USER_ID)


def test_project_service_member_unique_violation(monkeypatch) -> None:
    fake = FakeTransaction(
        fetchones=[
            {"role": "owner"},
            user_row(user_id=TARGET_USER_ID, email="member@example.com"),
        ],
        execute_errors=[None, None, UniqueViolation()],
    )
    monkeypatch.setattr(project_service, "transaction", fake.transaction)

    with pytest.raises(ApiError) as error:
        project_service.add_project_member(
            USER_ID,
            PROJECT_ID,
            "member@example.com",
            "member",
        )

    assert error.value.type == "validation_error"


def test_api_key_service_create_list_revoke(monkeypatch, app) -> None:
    fake = FakeTransaction(
        fetchones=[
            {"role": "member"},
            api_key_row(),
            {"role": "viewer"},
            {"role": "member"},
            {"api_key_id": API_KEY_ID, "revoked_at": NOW},
        ],
        fetchalls=[[api_key_row()]],
    )
    monkeypatch.setattr(api_key_service, "transaction", fake.transaction)
    monkeypatch.setattr(
        api_key_service.api_keys,
        "generate_api_key",
        lambda: ("mt_live_visible_secret", "mt_live_visible"),
    )
    monkeypatch.setattr(
        api_key_service.api_keys,
        "hash_api_key",
        lambda raw_key: "sha256:hash",
    )

    with app.app_context():
        created = api_key_service.create_api_key(USER_ID, PROJECT_ID, "Production")
        listed = api_key_service.list_api_keys(USER_ID, PROJECT_ID)
        revoked = api_key_service.revoke_api_key(USER_ID, PROJECT_ID, API_KEY_ID)

    assert created["api_key"] == "mt_live_visible_secret"
    assert listed["api_keys"][0]["apiKeyID"] == API_KEY_ID
    assert revoked == {"revoked": True}


def test_api_key_service_unique_and_not_found_branches(monkeypatch, app) -> None:
    fake = FakeTransaction(
        fetchones=[
            {"role": "member"},
            {"role": "member"},
            None,
        ],
        execute_errors=[None, UniqueViolation("uq_api_keys_project_name")],
    )
    monkeypatch.setattr(api_key_service, "transaction", fake.transaction)
    monkeypatch.setattr(
        api_key_service.api_keys,
        "generate_api_key",
        lambda: ("mt_live_visible_secret", "mt_live_visible"),
    )
    monkeypatch.setattr(
        api_key_service.api_keys,
        "hash_api_key",
        lambda raw_key: "sha256:hash",
    )

    with app.app_context(), pytest.raises(ApiError) as duplicate:
        api_key_service.create_api_key(USER_ID, PROJECT_ID, "Production")

    with pytest.raises(ApiError) as missing:
        api_key_service.revoke_api_key(USER_ID, PROJECT_ID, API_KEY_ID)

    assert duplicate.value.status_code == 409
    assert missing.value.type == "api_key_not_found"


def test_api_key_service_retries_generated_key_hash_collision(monkeypatch, app) -> None:
    fake = FakeTransaction(
        fetchones=[
            {"role": "member"},
            {"role": "member"},
            api_key_row(key_prefix="mt_live_second", key_hash="sha256:second"),
        ],
        execute_errors=[
            None,
            UniqueViolation("uq_api_keys_key_hash"),
            None,
            None,
        ],
    )
    generated_keys = iter(
        [
            ("mt_live_first_secret", "mt_live_first"),
            ("mt_live_second_secret", "mt_live_second"),
        ]
    )
    monkeypatch.setattr(api_key_service, "transaction", fake.transaction)
    monkeypatch.setattr(
        api_key_service.api_keys,
        "generate_api_key",
        lambda: next(generated_keys),
    )
    monkeypatch.setattr(
        api_key_service.api_keys,
        "hash_api_key",
        lambda raw_key: f"sha256:{raw_key}",
    )

    with app.app_context():
        created = api_key_service.create_api_key(USER_ID, PROJECT_ID, "Production")

    assert created["api_key"] == "mt_live_second_secret"
    assert fake.cursor.executed[1][1]["key_hash"] == "sha256:mt_live_first_secret"
    assert fake.cursor.executed[3][1]["key_hash"] == "sha256:mt_live_second_secret"


def test_api_key_service_fails_after_repeated_generated_key_hash_collisions(
    monkeypatch,
    app,
) -> None:
    fake = FakeTransaction(
        fetchones=[{"role": "member"}, {"role": "member"}, {"role": "member"}],
        execute_errors=[
            None,
            UniqueViolation("uq_api_keys_key_hash"),
            None,
            UniqueViolation("uq_api_keys_key_hash"),
            None,
            UniqueViolation("uq_api_keys_key_hash"),
        ],
    )
    monkeypatch.setattr(api_key_service, "transaction", fake.transaction)
    monkeypatch.setattr(
        api_key_service.api_keys,
        "generate_api_key",
        lambda: ("mt_live_visible_secret", "mt_live_visible"),
    )
    monkeypatch.setattr(
        api_key_service.api_keys,
        "hash_api_key",
        lambda raw_key: "sha256:hash",
    )

    with app.app_context(), pytest.raises(ApiError) as error:
        api_key_service.create_api_key(USER_ID, PROJECT_ID, "Production")

    assert error.value.type == "api_key_generation_failed"


def test_api_key_service_authenticate_project_api_key(monkeypatch, app) -> None:
    fake = FakeTransaction(fetchalls=[[api_key_row()]])
    monkeypatch.setattr(api_key_service, "transaction", fake.transaction)
    monkeypatch.setattr(
        api_key_service.api_keys,
        "verify_api_key",
        lambda raw_key, key_hash: True,
    )

    with app.app_context():
        identity = api_key_service.authenticate_project_api_key("mt_live_visible_secret")

    assert identity == {"apiKeyID": API_KEY_ID, "projectID": PROJECT_ID}


def test_api_key_service_authenticate_rejects_no_matching_hash(monkeypatch, app) -> None:
    fake = FakeTransaction(fetchalls=[[api_key_row()]])
    monkeypatch.setattr(api_key_service, "transaction", fake.transaction)
    monkeypatch.setattr(
        api_key_service.api_keys,
        "verify_api_key",
        lambda raw_key, key_hash: False,
    )

    with app.app_context(), pytest.raises(ApiError):
        api_key_service.authenticate_project_api_key("mt_live_visible_secret")


def test_model_deployment_service_create_list_get_commands(monkeypatch, app) -> None:
    fake = FakeTransaction(
        fetchones=[
            project_row(role="member"),
            deployment_row(),
            {"role": "viewer"},
            {"role": "viewer"},
            deployment_row(),
            {"role": "viewer"},
            deployment_row(),
            {"role": "member"},
            deployment_row(),
            deployment_row(status="deploying"),
            {"role": "member"},
            deployment_row(),
            deployment_row(status="stopped"),
            {"role": "member"},
            deployment_row(),
            deployment_row(replicas=3),
            {"role": "member"},
            deployment_row(),
            deployment_row(status="deleting"),
        ],
        fetchalls=[[deployment_row()], [job_row()]],
    )
    monkeypatch.setattr(model_deployment_service, "transaction", fake.transaction)
    monkeypatch.setattr(
        model_deployment_service,
        "enqueue_deployment_job_with_cursor",
        lambda cur, project_id, model_deployment_id, job_type, payload: job_row(
            job_type=job_type
        ),
    )

    with app.app_context():
        created = model_deployment_service.create_model_deployment(
            USER_ID,
            PROJECT_ID,
            {"name": "qwen-small-prod", "model_id": "Qwen/Qwen2.5-0.5B-Instruct"},
        )
        listed = model_deployment_service.list_model_deployments(USER_ID, PROJECT_ID)
        fetched = model_deployment_service.get_model_deployment(
            USER_ID,
            PROJECT_ID,
            MODEL_DEPLOYMENT_ID,
        )
        jobs = model_deployment_service.list_model_deployment_jobs(
            USER_ID,
            PROJECT_ID,
            MODEL_DEPLOYMENT_ID,
        )
        started = model_deployment_service.start_model_deployment(
            USER_ID,
            PROJECT_ID,
            MODEL_DEPLOYMENT_ID,
        )
        stopped = model_deployment_service.stop_model_deployment(
            USER_ID,
            PROJECT_ID,
            MODEL_DEPLOYMENT_ID,
        )
        scaled = model_deployment_service.scale_model_deployment(
            USER_ID,
            PROJECT_ID,
            MODEL_DEPLOYMENT_ID,
            3,
        )
        deleted = model_deployment_service.delete_model_deployment(
            USER_ID,
            PROJECT_ID,
            MODEL_DEPLOYMENT_ID,
        )

    assert created["deploymentJob"]["job_type"] == "deploy_model"
    assert listed["modelDeployments"][0]["modelDeploymentID"] == MODEL_DEPLOYMENT_ID
    assert fetched["modelDeploymentID"] == MODEL_DEPLOYMENT_ID
    assert jobs["deploymentJobs"][0]["deploymentJobID"] == JOB_ID
    assert started["deploymentJob"]["job_type"] == "start_model"
    assert stopped["deploymentJob"]["job_type"] == "stop_model"
    assert scaled["modelDeployment"]["replicas"] == 3
    assert deleted["deploymentJob"]["job_type"] == "delete_model"


def test_model_deployment_service_error_branches(monkeypatch, app) -> None:
    fake = FakeTransaction(
        fetchones=[
            project_row(role="member"),
            {"role": "member"},
            None,
            {"role": "member"},
            None,
        ],
        execute_errors=[None, UniqueViolation()],
    )
    monkeypatch.setattr(model_deployment_service, "transaction", fake.transaction)

    with app.app_context(), pytest.raises(ApiError) as duplicate:
        model_deployment_service.create_model_deployment(
            USER_ID,
            PROJECT_ID,
            {"name": "qwen-small-prod", "model_id": "Qwen/Qwen2.5-0.5B-Instruct"},
        )

    with pytest.raises(ApiError) as scale_missing:
        model_deployment_service.scale_model_deployment(
            USER_ID,
            PROJECT_ID,
            MODEL_DEPLOYMENT_ID,
            1,
        )

    with pytest.raises(ApiError) as lifecycle_missing:
        model_deployment_service.start_model_deployment(
            USER_ID,
            PROJECT_ID,
            MODEL_DEPLOYMENT_ID,
        )

    assert duplicate.value.status_code == 409
    assert scale_missing.value.type == "model_deployment_not_found"
    assert lifecycle_missing.value.type == "model_deployment_not_found"


class FakeDiag:
    def __init__(self, constraint_name=None):
        self.constraint_name = constraint_name


class UniqueViolation(Exception):
    def __init__(self, constraint_name=None):
        super().__init__(constraint_name)
        self.diag = FakeDiag(constraint_name)


class FakeCursor:
    def __init__(self, *, fetchones=None, fetchalls=None, execute_errors=None):
        self.fetchones = list(fetchones or [])
        self.fetchalls = list(fetchalls or [])
        self.execute_errors = list(execute_errors or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.execute_errors:
            error = self.execute_errors.pop(0)
            if error is not None:
                raise error

    def fetchone(self):
        return self.fetchones.pop(0) if self.fetchones else None

    def fetchall(self):
        return self.fetchalls.pop(0) if self.fetchalls else []


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    @contextmanager
    def cursor(self):
        yield self._cursor


class FakeTransaction:
    def __init__(self, *, fetchones=None, fetchalls=None, execute_errors=None):
        self.cursor = FakeCursor(
            fetchones=fetchones,
            fetchalls=fetchalls,
            execute_errors=execute_errors,
        )

    @contextmanager
    def transaction(self):
        yield FakeConnection(self.cursor)
