"""Project and project membership route/service tests."""

from datetime import UTC, datetime

import pytest

from app import create_app
from app.security.tokens import create_access_token
from app.services import project_service
from app.services.project_service import (
    ensure_not_last_owner,
    require_role,
    serialize_member,
    serialize_project,
)
from app.utils.errors import ApiError


class TestConfig:
    TESTING = True
    SECRET_KEY = "test-secret-key-change-me-32-bytes"


@pytest.fixture
def app():
    return create_app(TestConfig)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_headers(app):
    with app.app_context():
        token = create_access_token("9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e")
    return {"Authorization": f"Bearer {token}"}


def project_response() -> dict[str, object]:
    return {
        "projectID": "a2fc41b7-862e-4060-b466-2376f29227bb",
        "name": "Personal Models",
        "slug": "personal-models",
        "k8s_namespace": "miniten-personal-models",
        "created_at": "2026-05-17T12:00:00Z",
        "role": "owner",
    }


def member_response(role: str = "member") -> dict[str, object]:
    return {
        "userID": "5bcb59e9-86e8-4ac9-b5c6-66f332697a0c",
        "email": "member@example.com",
        "role": role,
        "created_at": "2026-05-17T12:10:00Z",
    }


def test_create_project_route(monkeypatch, client, auth_headers) -> None:
    def create_project(user_id, name):
        assert user_id == "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e"
        assert name == "Personal Models"
        return project_response()

    monkeypatch.setattr(
        "app.routes.projects.project_service.create_project",
        create_project,
    )

    response = client.post(
        "/v1/projects",
        json={"name": "Personal Models"},
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert response.get_json() == project_response()


def test_list_projects_route(monkeypatch, client, auth_headers) -> None:
    expected = {"projects": [project_response()]}
    monkeypatch.setattr(
        "app.routes.projects.project_service.list_projects",
        lambda user_id: expected,
    )

    response = client.get("/v1/projects", headers=auth_headers)

    assert response.status_code == 200
    assert response.get_json() == expected


def test_get_project_route(monkeypatch, client, auth_headers) -> None:
    monkeypatch.setattr(
        "app.routes.projects.project_service.get_project",
        lambda user_id, project_id: project_response(),
    )

    response = client.get(
        "/v1/projects/a2fc41b7-862e-4060-b466-2376f29227bb",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == project_response()


def test_delete_project_route(monkeypatch, client, auth_headers) -> None:
    monkeypatch.setattr(
        "app.routes.projects.project_service.delete_project",
        lambda user_id, project_id: {"deleted": True},
    )

    response = client.delete(
        "/v1/projects/a2fc41b7-862e-4060-b466-2376f29227bb",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {"deleted": True}


def test_project_routes_require_auth(client) -> None:
    response = client.get("/v1/projects")

    assert response.status_code == 401
    assert response.get_json()["error"]["type"] == "unauthorized"


def test_truss_init_project_route_uses_account_api_key(monkeypatch, client) -> None:
    monkeypatch.setattr(
        "app.routes.truss.account_api_key_service.authenticate_account_api_key",
        lambda raw_key: {
            "accountApiKeyID": "key-id",
            "userID": "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e",
            "apiKeyPrefix": "mt_live",
        },
    )

    def create_if_missing(user_id, name):
        assert user_id == "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e"
        assert name == "qwen-2.5-3b"
        return {**project_response(), "name": name}

    monkeypatch.setattr(
        "app.routes.truss.project_service.create_project_if_missing",
        create_if_missing,
    )

    response = client.post(
        "/v1/truss/projects/init",
        json={"name": "qwen-2.5-3b"},
        headers={"Authorization": "Bearer mt_live_account_key"},
    )

    assert response.status_code == 200
    assert response.get_json()["project"]["name"] == "qwen-2.5-3b"


def test_list_project_members_route(monkeypatch, client, auth_headers) -> None:
    expected = {"members": [member_response()]}
    monkeypatch.setattr(
        "app.routes.project_members.project_service.list_project_members",
        lambda user_id, project_id: expected,
    )

    response = client.get(
        "/v1/projects/a2fc41b7-862e-4060-b466-2376f29227bb/members",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == expected


def test_add_project_member_route(monkeypatch, client, auth_headers) -> None:
    def add_project_member(user_id, project_id, email, role):
        assert email == "member@example.com"
        assert role == "member"
        return member_response()

    monkeypatch.setattr(
        "app.routes.project_members.project_service.add_project_member",
        add_project_member,
    )

    response = client.post(
        "/v1/projects/a2fc41b7-862e-4060-b466-2376f29227bb/members",
        json={"email": "member@example.com", "role": "member"},
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert response.get_json() == member_response()


def test_update_project_member_route(monkeypatch, client, auth_headers) -> None:
    monkeypatch.setattr(
        "app.routes.project_members.project_service.update_project_member_role",
        lambda user_id, project_id, target_user_id, role: member_response("viewer"),
    )

    response = client.patch(
        "/v1/projects/a2fc41b7-862e-4060-b466-2376f29227bb/"
        "members/5bcb59e9-86e8-4ac9-b5c6-66f332697a0c",
        json={"role": "viewer"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == member_response("viewer")


def test_remove_project_member_route(monkeypatch, client, auth_headers) -> None:
    monkeypatch.setattr(
        "app.routes.project_members.project_service.remove_project_member",
        lambda user_id, project_id, target_user_id: {"removed": True},
    )

    response = client.delete(
        "/v1/projects/a2fc41b7-862e-4060-b466-2376f29227bb/"
        "members/5bcb59e9-86e8-4ac9-b5c6-66f332697a0c",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {"removed": True}


def test_serialize_project() -> None:
    row = {
        "project_id": "a2fc41b7-862e-4060-b466-2376f29227bb",
        "name": "Personal Models",
        "slug": "personal-models",
        "k8s_namespace": "miniten-personal-models",
        "created_at": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
    }

    assert serialize_project(row, role="owner") == project_response()


def test_serialize_member() -> None:
    row = {
        "user_id": "5bcb59e9-86e8-4ac9-b5c6-66f332697a0c",
        "email": "member@example.com",
        "role": "member",
        "created_at": datetime(2026, 5, 17, 12, 10, tzinfo=UTC),
    }

    assert serialize_member(row) == member_response()


def test_require_role_rejects_missing_and_forbidden_roles() -> None:
    with pytest.raises(ApiError) as missing_error:
        require_role(None, {"owner"})

    assert missing_error.value.message == "Project not found."

    with pytest.raises(ApiError) as forbidden_error:
        require_role("viewer", {"owner"})

    assert forbidden_error.value.type == "forbidden"


def test_ensure_not_last_owner_rejects_last_owner() -> None:
    class Cursor:
        def execute(self, sql, params):
            self.sql = sql
            self.params = params

        def fetchone(self):
            return {"owner_count": 1}

    with pytest.raises(ApiError) as error:
        ensure_not_last_owner(Cursor(), "a2fc41b7-862e-4060-b466-2376f29227bb")

    assert error.value.message == "A project must have at least one owner."


def test_delete_project_queues_namespace_cleanup_before_deleting_metadata(monkeypatch) -> None:
    cursor = FakeProjectCursor(
        fetchones=[
            {"role": "owner"},
            {
                "project_id": "a2fc41b7-862e-4060-b466-2376f29227bb",
                "name": "Personal Models",
                "slug": "personal-models",
                "k8s_namespace": "miniten-personal-models",
                "created_at": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
                "role": "owner",
            },
            {
                "project_cleanup_job_id": "5d6ff43f-bb5b-4373-bfea-22da7e0c8765",
            },
            {
                "project_id": "a2fc41b7-862e-4060-b466-2376f29227bb",
                "k8s_namespace": "miniten-personal-models",
            },
        ],
    )
    monkeypatch.setattr(project_service, "transaction", FakeProjectTransaction(cursor))

    response = project_service.delete_project(
        "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e",
        "a2fc41b7-862e-4060-b466-2376f29227bb",
    )

    assert response == {"deleted": True}
    cleanup_index = next(
        index
        for index, params in enumerate(cursor.executed)
        if params.get("k8s_namespace") == "miniten-personal-models"
    )
    delete_index = next(
        index
        for index, params in enumerate(cursor.executed)
        if list(params) == ["project_id"]
    )
    assert cleanup_index < delete_index


class FakeProjectCursor:
    def __init__(self, fetchones):
        self.fetchones = list(fetchones)
        self.executed = []

    def execute(self, _sql, params):
        self.executed.append(params)

    def fetchone(self):
        return self.fetchones.pop(0) if self.fetchones else None

    def fetchall(self):
        return []


class FakeProjectConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return FakeProjectCursorContext(self._cursor)


class FakeProjectCursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, *args):
        return False


class FakeProjectTransaction:
    def __init__(self, cursor):
        self.cursor = cursor

    def __call__(self):
        return self

    def __enter__(self):
        return FakeProjectConnection(self.cursor)

    def __exit__(self, *args):
        return False
