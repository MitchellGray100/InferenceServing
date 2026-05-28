"""Project API key route, service, and security tests."""

from datetime import UTC, datetime

import pytest

from app import create_app
from app.security import api_keys
from app.security.tokens import create_access_token
from app.services.api_key_service import (
    api_key_not_found_error,
    serialize_api_key,
)


PROJECT_ID = "a2fc41b7-862e-4060-b466-2376f29227bb"
API_KEY_ID = "4c659341-7357-42f6-9ee8-801a1f340b35"
USER_ID = "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e"


class TestConfig:
    TESTING = True
    SECRET_KEY = "test-secret-key-change-me-32-bytes"
    API_KEY_HASH_SECRET = "test-api-key-hash-secret-change-me-32-bytes"


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


def api_key_response(include_raw_key: bool = False) -> dict[str, object]:
    response = {
        "apiKeyID": API_KEY_ID,
        "projectID": PROJECT_ID,
        "name": "Production",
        "key_prefix": "mt_live_visiblepart",
        "created_at": "2026-05-17T12:00:00Z",
        "last_used_at": None,
        "revoked_at": None,
    }

    if include_raw_key:
        response["api_key"] = "mt_live_visiblepart_secretpart"

    return response


def test_generate_api_key_returns_raw_key_and_visible_prefix(app) -> None:
    with app.app_context():
        raw_key, key_prefix = api_keys.generate_api_key()

    assert raw_key.startswith("mt_live_")
    assert key_prefix.startswith("mt_live_")
    assert api_keys.derive_key_prefix(raw_key) == key_prefix


def test_api_key_hash_roundtrip(app) -> None:
    with app.app_context():
        raw_key, _ = api_keys.generate_api_key()
        key_hash = api_keys.hash_api_key(raw_key)

    assert key_hash.startswith("sha256:")
    assert api_keys.verify_api_key(raw_key, key_hash, TestConfig.API_KEY_HASH_SECRET)
    assert not api_keys.verify_api_key(
        f"{raw_key}-wrong",
        key_hash,
        TestConfig.API_KEY_HASH_SECRET,
    )


def test_invalid_api_key_prefix_is_unauthorized() -> None:
    with pytest.raises(Exception) as error:
        api_keys.derive_key_prefix("invalid")

    assert error.value.type == "unauthorized"


def test_create_api_key_route(monkeypatch, client, auth_headers) -> None:
    def create_api_key(user_id, project_id, name):
        assert user_id == USER_ID
        assert project_id == PROJECT_ID
        assert name == "Production"
        return api_key_response(include_raw_key=True)

    monkeypatch.setattr(
        "app.routes.api_keys.api_key_service.create_api_key",
        create_api_key,
    )

    response = client.post(
        f"/v1/projects/{PROJECT_ID}/api-keys",
        json={"name": "Production"},
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert response.get_json() == api_key_response(include_raw_key=True)


def test_list_api_keys_route(monkeypatch, client, auth_headers) -> None:
    expected = {"api_keys": [api_key_response()]}
    monkeypatch.setattr(
        "app.routes.api_keys.api_key_service.list_api_keys",
        lambda user_id, project_id: expected,
    )

    response = client.get(
        f"/v1/projects/{PROJECT_ID}/api-keys",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == expected


def test_revoke_api_key_route(monkeypatch, client, auth_headers) -> None:
    monkeypatch.setattr(
        "app.routes.api_keys.api_key_service.revoke_api_key",
        lambda user_id, project_id, api_key_id: {"revoked": True},
    )

    response = client.delete(
        f"/v1/projects/{PROJECT_ID}/api-keys/{API_KEY_ID}",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {"revoked": True}


def test_api_key_routes_require_auth(client) -> None:
    response = client.get(f"/v1/projects/{PROJECT_ID}/api-keys")

    assert response.status_code == 401
    assert response.get_json()["error"]["type"] == "unauthorized"


def test_serialize_api_key_does_not_expose_hash() -> None:
    row = {
        "api_key_id": API_KEY_ID,
        "project_id": PROJECT_ID,
        "name": "Production",
        "key_prefix": "mt_live_visiblepart",
        "key_hash": "sha256:secret",
        "created_at": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
        "last_used_at": None,
        "revoked_at": None,
    }

    response = serialize_api_key(row)

    assert response == api_key_response()
    assert "key_hash" not in response


def test_api_key_not_found_error() -> None:
    error = api_key_not_found_error()

    assert error.type == "api_key_not_found"
    assert error.status_code == 404
