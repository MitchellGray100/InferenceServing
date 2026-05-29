import pytest

from app import create_app
from app.security.passwords import hash_password, verify_password
from app.security.tokens import create_access_token, decode_access_token


class TestConfig:
    TESTING = True
    SECRET_KEY = "test-secret-key-change-me-32-bytes"


@pytest.fixture
def app():
    return create_app(TestConfig)


@pytest.fixture
def client(app):
    return app.test_client()


def user_response() -> dict[str, object]:
    return {
        "userID": "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e",
        "email": "user@example.com",
        "created_at": "2026-05-17T12:00:00Z",
        "last_login_at": None,
    }


def test_password_hash_and_verify_roundtrip() -> None:
    hashed = hash_password("password123")

    assert hashed != "password123"
    assert verify_password("password123", hashed)
    assert not verify_password("wrong-password", hashed)


def test_access_token_roundtrip(app) -> None:
    with app.app_context():
        token = create_access_token(
            "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e",
            token_version=2,
        )
        payload = decode_access_token(token)

    assert payload["sub"] == "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e"
    assert payload["type"] == "user_access"
    assert payload["token_version"] == 2
    assert isinstance(payload["exp"], int)


def test_create_user_route(monkeypatch, client) -> None:
    def create_user(email, password):
        assert email == "user@example.com"
        assert password == "password123"
        return user_response()

    monkeypatch.setattr("app.routes.users.user_service.create_user", create_user)

    response = client.post(
        "/v1/users",
        json={"email": "user@example.com", "password": "password123"},
    )

    assert response.status_code == 201
    assert response.get_json() == user_response()


def test_create_user_route_rejects_missing_json(client) -> None:
    response = client.post("/v1/users")

    assert response.status_code == 400
    assert response.get_json()["error"]["type"] == "validation_error"


def test_get_current_user_route(monkeypatch, app, client) -> None:
    def get_user(user_id):
        assert user_id == "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e"
        return user_response()

    monkeypatch.setattr("app.routes.users.user_service.get_user", get_user)

    with app.app_context():
        token = create_access_token("9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e")

    response = client.get(
        "/v1/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.get_json() == user_response()


def test_get_current_user_route_requires_auth(client) -> None:
    response = client.get("/v1/users/me")

    assert response.status_code == 401
    assert response.get_json()["error"]["type"] == "unauthorized"


def test_delete_current_user_route(monkeypatch, app, client) -> None:
    monkeypatch.setattr(
        "app.routes.users.user_service.delete_user",
        lambda user_id: {"deleted": True},
    )

    with app.app_context():
        token = create_access_token("9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e")

    response = client.delete(
        "/v1/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"deleted": True}


def test_login_route(monkeypatch, client) -> None:
    expected = {
        "access_token": "jwt",
        "token_type": "bearer",
        "user": user_response(),
    }

    def login(email, password):
        assert email == "user@example.com"
        assert password == "password123"
        return expected

    monkeypatch.setattr("app.routes.auth.auth_service.login", login)

    response = client.post(
        "/v1/auth/login",
        json={"email": "user@example.com", "password": "password123"},
    )

    assert response.status_code == 200
    assert response.get_json() == expected


def test_logout_route(monkeypatch, app, client) -> None:
    calls = []

    def logout(user_id):
        calls.append(user_id)
        return {"logged_out": True}

    monkeypatch.setattr("app.routes.auth.auth_service.logout", logout)
    with app.app_context():
        token = create_access_token("9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e")

    response = client.post(
        "/v1/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"logged_out": True}
    assert calls == ["9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e"]
