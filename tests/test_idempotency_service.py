"""Control-plane idempotency service tests."""

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from app.services import idempotency_service
from app.utils.errors import ApiError


PROJECT_ID = "a2fc41b7-862e-4060-b466-2376f29227bb"
USER_ID = "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e"
IDEMPOTENCY_KEY_ID = "f85bf1e3-5807-44f5-9f49-f28651384052"
NOW = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)


def idempotency_row(**overrides):
    row = {
        "idempotency_key_id": IDEMPOTENCY_KEY_ID,
        "project_id": PROJECT_ID,
        "user_id": USER_ID,
        "idempotency_key": "deploy-qwen",
        "request_hash": idempotency_service.build_request_hash(
            method="POST",
            path="/v1/projects/project/models",
            body={"name": "qwen"},
        ),
        "response_status": None,
        "response_body": None,
        "created_at": NOW,
        "expires_at": NOW,
    }
    row.update(overrides)
    return row


def test_run_idempotent_control_plane_request_requires_key() -> None:
    with pytest.raises(ApiError) as error:
        idempotency_service.run_idempotent_control_plane_request(
            project_id=PROJECT_ID,
            user_id=USER_ID,
            idempotency_key=None,
            method="POST",
            path="/v1/projects/project/models",
            body={"name": "qwen"},
            action=lambda: pytest.fail("missing key should not run action"),
        )

    assert error.value.type == "missing_idempotency_key"


def test_run_idempotent_control_plane_request_replays_saved_response(monkeypatch) -> None:
    request_hash = idempotency_service.build_request_hash(
        method="POST",
        path="/v1/projects/project/models",
        body={"name": "qwen"},
    )
    fake = FakeTransaction(
        fetchones=[
            idempotency_row(
                request_hash=request_hash,
                response_status=201,
                response_body={"created": True},
            )
        ]
    )
    monkeypatch.setattr(idempotency_service, "transaction", fake.transaction)

    response = idempotency_service.run_idempotent_control_plane_request(
        project_id=PROJECT_ID,
        user_id=USER_ID,
        idempotency_key="deploy-qwen",
        method="POST",
        path="/v1/projects/project/models",
        body={"name": "qwen"},
        action=lambda: pytest.fail("saved response should be replayed"),
    )

    assert response == ({"created": True}, 201)


def test_run_idempotent_control_plane_request_rejects_conflicting_request(
    monkeypatch,
) -> None:
    fake = FakeTransaction(fetchones=[idempotency_row(request_hash="different")])
    monkeypatch.setattr(idempotency_service, "transaction", fake.transaction)

    with pytest.raises(ApiError) as error:
        idempotency_service.run_idempotent_control_plane_request(
            project_id=PROJECT_ID,
            user_id=USER_ID,
            idempotency_key="deploy-qwen",
            method="POST",
            path="/v1/projects/project/models",
            body={"name": "qwen"},
            action=lambda: ({"created": True}, 201),
        )

    assert error.value.type == "idempotency_key_conflict"


def test_run_idempotent_control_plane_request_saves_new_response(monkeypatch) -> None:
    fake = FakeTransaction(fetchones=[None, idempotency_row(), idempotency_row()])
    monkeypatch.setattr(idempotency_service, "transaction", fake.transaction)

    response = idempotency_service.run_idempotent_control_plane_request(
        project_id=PROJECT_ID,
        user_id=USER_ID,
        idempotency_key="deploy-qwen",
        method="POST",
        path="/v1/projects/project/models",
        body={"name": "qwen"},
        action=lambda: ({"created": True}, 201),
    )

    assert response == ({"created": True}, 201)
    assert fake.cursor.executed[-1][1]["response_status"] == 201


class FakeCursor:
    def __init__(self, *, fetchones=None):
        self.fetchones = list(fetchones or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.fetchones.pop(0) if self.fetchones else None


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
