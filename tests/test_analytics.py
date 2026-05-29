"""Analytics route and service tests."""

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from app import create_app
from app.security.tokens import create_access_token
from app.services import analytics_service
from app.utils.errors import ApiError


PROJECT_ID = "a2fc41b7-862e-4060-b466-2376f29227bb"
USER_ID = "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e"
MODEL_DEPLOYMENT_ID = "bf3dc090-5bb4-46f6-964d-6cd8375ddf56"
INFERENCE_REQUEST_ID = "67e77e15-90c8-4fbb-9eee-f4b79bc3d050"
API_KEY_ID = "4c659341-7357-42f6-9ee8-801a1f340b35"
MODEL_EVENT_ID = "36e4e316-6d8c-40f9-aef3-476d9b46ecbc"
NOW = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)


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
        token = create_access_token(USER_ID)
    return {"Authorization": f"Bearer {token}"}


def test_get_model_metrics_route(monkeypatch, client, auth_headers) -> None:
    """The metrics route passes auth identity, path params, and query params."""
    expected = {
        "model": {
            "name": "qwen-small-prod",
            "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
            "status": "running",
        },
        "metrics": {
            "request_count": 10,
            "success_count": 9,
            "error_count": 1,
            "average_latency_ms": 780,
            "p95_latency_ms": 1200,
            "last_request_at": "2026-05-17T12:00:00Z",
        },
    }

    def get_model_metrics(user_id, project_id, model_name, since):
        assert user_id == USER_ID
        assert project_id == PROJECT_ID
        assert model_name == "qwen-small-prod"
        assert since == "2026-05-17T00:00:00Z"
        return expected

    monkeypatch.setattr(
        "app.routes.analytics.analytics_service.get_model_metrics",
        get_model_metrics,
    )

    response = client.get(
        f"/v1/projects/{PROJECT_ID}/analytics/models/qwen-small-prod/metrics"
        "?since=2026-05-17T00:00:00Z",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == expected


def test_get_project_overview_route(monkeypatch, client, auth_headers) -> None:
    """The overview route returns project-level analytics."""
    expected = {
        "projectID": PROJECT_ID,
        "summary": {
            "total_models": 1,
            "running_models": 1,
            "stopped_models": 0,
            "total_requests": 10,
            "error_count": 1,
            "average_latency_ms": 100,
            "last_request_at": "2026-05-17T12:00:00Z",
        },
        "models": [],
    }

    def get_project_overview(user_id, project_id):
        assert user_id == USER_ID
        assert project_id == PROJECT_ID
        return expected

    monkeypatch.setattr(
        "app.routes.analytics.analytics_service.get_project_overview",
        get_project_overview,
    )

    response = client.get(
        f"/v1/projects/{PROJECT_ID}/analytics/overview",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == expected


def test_list_model_requests_route(monkeypatch, client, auth_headers) -> None:
    """The request-history route forwards filters to the service layer."""
    expected = {"requests": [request_response()]}

    def list_model_requests(user_id, project_id, model_name, limit, status_code, since):
        assert user_id == USER_ID
        assert project_id == PROJECT_ID
        assert model_name == "qwen-small-prod"
        assert limit == "50"
        assert status_code == "500"
        assert since == "2026-05-17T00:00:00Z"
        return expected

    monkeypatch.setattr(
        "app.routes.analytics.analytics_service.list_model_requests",
        list_model_requests,
    )

    response = client.get(
        f"/v1/projects/{PROJECT_ID}/analytics/models/qwen-small-prod/requests"
        "?limit=50&status_code=500&since=2026-05-17T00:00:00Z",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == expected


def test_list_model_events_route(monkeypatch, client, auth_headers) -> None:
    """The events route returns lifecycle history for a named deployment."""
    expected = {"events": [event_response()]}

    def list_model_events(user_id, project_id, model_name):
        assert user_id == USER_ID
        assert project_id == PROJECT_ID
        assert model_name == "qwen-small-prod"
        return expected

    monkeypatch.setattr(
        "app.routes.analytics.analytics_service.list_model_events",
        list_model_events,
    )

    response = client.get(
        f"/v1/projects/{PROJECT_ID}/analytics/models/qwen-small-prod/events",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == expected


def test_analytics_routes_require_user_auth(client) -> None:
    """Analytics endpoints are dashboard reads and require user JWT auth."""
    response = client.get(
        f"/v1/projects/{PROJECT_ID}/analytics/models/qwen-small-prod/metrics"
    )

    assert response.status_code == 401
    assert response.get_json()["error"]["type"] == "unauthorized"


def test_get_model_metrics_service(monkeypatch) -> None:
    """Metrics service authorizes, resolves the deployment, then aggregates."""
    fake = FakeTransaction(
        fetchones=[
            {"role": "viewer"},
            deployment_row(),
            {
                "request_count": 10,
                "success_count": 9,
                "error_count": 1,
                "average_latency_ms": 780,
                "p95_latency_ms": 1200,
                "last_request_at": NOW,
            },
        ]
    )
    monkeypatch.setattr(analytics_service, "transaction", fake.transaction)

    response = analytics_service.get_model_metrics(
        USER_ID,
        PROJECT_ID,
        "qwen-small-prod",
        "2026-05-17T00:00:00Z",
    )

    assert response["model"]["status"] == "running"
    assert response["metrics"]["p95_latency_ms"] == 1200
    assert response["metrics"]["last_request_at"] == "2026-05-17T12:00:00Z"
    assert fake.cursor.executed[-1][1]["since"] == datetime(
        2026,
        5,
        17,
        tzinfo=UTC,
    )


def test_get_model_metrics_defaults_empty_aggregate(monkeypatch) -> None:
    """Empty metric aggregates serialize counts as zero and timestamps as null."""
    fake = FakeTransaction(
        fetchones=[
            {"role": "viewer"},
            deployment_row(),
            {
                "request_count": 0,
                "success_count": 0,
                "error_count": 0,
                "average_latency_ms": None,
                "p95_latency_ms": None,
                "last_request_at": None,
            },
        ]
    )
    monkeypatch.setattr(analytics_service, "transaction", fake.transaction)

    response = analytics_service.get_model_metrics(
        USER_ID,
        PROJECT_ID,
        "qwen-small-prod",
    )

    assert response["metrics"] == {
        "request_count": 0,
        "success_count": 0,
        "error_count": 0,
        "average_latency_ms": None,
        "p95_latency_ms": None,
        "last_request_at": None,
    }


def test_get_project_overview_service(monkeypatch) -> None:
    """Overview service authorizes once and aggregates per-model rows."""
    fake = FakeTransaction(
        fetchones=[{"role": "viewer"}],
        fetchalls=[
            [
                {
                    "name": "qwen-small-prod",
                    "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                    "status": "running",
                    "request_count": 10,
                    "error_count": 1,
                    "average_latency_ms": 100,
                    "last_request_at": NOW,
                },
                {
                    "name": "qwen-small-dev",
                    "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                    "status": "stopped",
                    "request_count": 0,
                    "error_count": 0,
                    "average_latency_ms": None,
                    "last_request_at": None,
                },
            ]
        ],
    )
    monkeypatch.setattr(analytics_service, "transaction", fake.transaction)

    response = analytics_service.get_project_overview(USER_ID, PROJECT_ID)

    assert response["summary"]["total_models"] == 2
    assert response["summary"]["running_models"] == 1
    assert response["summary"]["stopped_models"] == 1
    assert response["summary"]["total_requests"] == 10
    assert response["summary"]["error_count"] == 1
    assert response["summary"]["last_request_at"] == "2026-05-17T12:00:00Z"


def test_list_model_requests_service(monkeypatch) -> None:
    """Request-history service applies filters and serializes safe metadata."""
    fake = FakeTransaction(
        fetchones=[{"role": "member"}, deployment_row()],
        fetchalls=[[request_row()]],
    )
    monkeypatch.setattr(analytics_service, "transaction", fake.transaction)

    response = analytics_service.list_model_requests(
        USER_ID,
        PROJECT_ID,
        "qwen-small-prod",
        limit="50",
        status_code="500",
        since="2026-05-17T00:00:00Z",
    )

    assert response == {"requests": [request_response()]}
    assert fake.cursor.executed[-1][1]["limit"] == 50
    assert fake.cursor.executed[-1][1]["status_code"] == 500


def test_list_model_requests_service_uses_default_limit(monkeypatch) -> None:
    """Omitting limit uses the documented default request-history limit."""
    fake = FakeTransaction(
        fetchones=[{"role": "viewer"}, deployment_row()],
        fetchalls=[[]],
    )
    monkeypatch.setattr(analytics_service, "transaction", fake.transaction)

    response = analytics_service.list_model_requests(
        USER_ID,
        PROJECT_ID,
        "qwen-small-prod",
    )

    assert response == {"requests": []}
    assert fake.cursor.executed[-1][1]["limit"] == 100


def test_list_model_events_service(monkeypatch) -> None:
    """Events service returns deployment lifecycle event history."""
    fake = FakeTransaction(
        fetchones=[{"role": "owner"}, deployment_row()],
        fetchalls=[[event_row()]],
    )
    monkeypatch.setattr(analytics_service, "transaction", fake.transaction)

    response = analytics_service.list_model_events(
        USER_ID,
        PROJECT_ID,
        "qwen-small-prod",
    )

    assert response == {"events": [event_response()]}


def test_analytics_service_rejects_missing_deployment(monkeypatch) -> None:
    """Analytics reads fail with model_deployment_not_found for bad names."""
    fake = FakeTransaction(fetchones=[{"role": "viewer"}, None])
    monkeypatch.setattr(analytics_service, "transaction", fake.transaction)

    with pytest.raises(ApiError) as error:
        analytics_service.get_model_metrics(USER_ID, PROJECT_ID, "missing-model")

    assert error.value.type == "model_deployment_not_found"


def test_analytics_service_rejects_missing_project_membership(monkeypatch) -> None:
    """Project authorization is enforced before model lookup."""
    fake = FakeTransaction(fetchones=[None])
    monkeypatch.setattr(analytics_service, "transaction", fake.transaction)

    with pytest.raises(ApiError) as error:
        analytics_service.list_model_events(USER_ID, PROJECT_ID, "qwen-small-prod")

    assert error.value.type == "project_not_found"


@pytest.mark.parametrize("bad_limit", ["0", "501", "abc"])
def test_list_model_requests_rejects_bad_limit(monkeypatch, bad_limit) -> None:
    """Limit must be a bounded positive integer query parameter."""
    monkeypatch.setattr(
        analytics_service,
        "transaction",
        FakeTransaction().transaction,
    )

    with pytest.raises(ApiError) as error:
        analytics_service.list_model_requests(
            USER_ID,
            PROJECT_ID,
            "qwen-small-prod",
            limit=bad_limit,
        )

    assert error.value.type == "validation_error"


@pytest.mark.parametrize("bad_status_code", ["99", "600", "abc"])
def test_list_model_requests_rejects_bad_status_code(
    monkeypatch,
    bad_status_code,
) -> None:
    """Status-code filters must stay in the valid HTTP status range."""
    monkeypatch.setattr(
        analytics_service,
        "transaction",
        FakeTransaction().transaction,
    )

    with pytest.raises(ApiError) as error:
        analytics_service.list_model_requests(
            USER_ID,
            PROJECT_ID,
            "qwen-small-prod",
            status_code=bad_status_code,
        )

    assert error.value.type == "validation_error"


def test_parse_optional_iso8601_rejects_bad_timestamp() -> None:
    """Invalid timestamp query parameters fail before DB access."""
    with pytest.raises(ApiError) as error:
        analytics_service.parse_optional_iso8601("not-a-time", "since")

    assert error.value.type == "validation_error"


def deployment_row() -> dict[str, object]:
    return {
        "model_deployment_id": MODEL_DEPLOYMENT_ID,
        "project_id": PROJECT_ID,
        "name": "qwen-small-prod",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "status": "running",
    }


def request_row() -> dict[str, object]:
    return {
        "inference_request_id": INFERENCE_REQUEST_ID,
        "project_id": PROJECT_ID,
        "model_deployment_id": MODEL_DEPLOYMENT_ID,
        "api_key_id": API_KEY_ID,
        "api_key_name": "production",
        "api_key_prefix": "mt_live",
        "status_code": 500,
        "latency_ms": 42,
        "error_type": "upstream_error",
        "request_path": "/v1/chat/completions",
        "method": "POST",
        "streamed": False,
        "created_at": NOW,
    }


def request_response() -> dict[str, object]:
    return {
        "inferenceRequestID": INFERENCE_REQUEST_ID,
        "projectID": PROJECT_ID,
        "modelDeploymentID": MODEL_DEPLOYMENT_ID,
        "apiKeyID": API_KEY_ID,
        "apiKeyName": "production",
        "apiKeyPrefix": "mt_live",
        "status_code": 500,
        "latency_ms": 42,
        "error_type": "upstream_error",
        "request_path": "/v1/chat/completions",
        "method": "POST",
        "streamed": False,
        "created_at": "2026-05-17T12:00:00Z",
    }


def event_row() -> dict[str, object]:
    return {
        "model_event_id": MODEL_EVENT_ID,
        "model_deployment_id": MODEL_DEPLOYMENT_ID,
        "project_id": PROJECT_ID,
        "event_type": "deploy_requested",
        "message": "Deployment requested for qwen-small-prod",
        "metadata": {"model_id": "Qwen/Qwen2.5-0.5B-Instruct"},
        "created_at": NOW,
    }


def event_response() -> dict[str, object]:
    return {
        "modelEventID": MODEL_EVENT_ID,
        "modelDeploymentID": MODEL_DEPLOYMENT_ID,
        "projectID": PROJECT_ID,
        "event_type": "deploy_requested",
        "message": "Deployment requested for qwen-small-prod",
        "metadata": {"model_id": "Qwen/Qwen2.5-0.5B-Instruct"},
        "created_at": "2026-05-17T12:00:00Z",
    }


class FakeCursor:
    def __init__(self, *, fetchones=None, fetchalls=None):
        self.fetchones = list(fetchones or [])
        self.fetchalls = list(fetchalls or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

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
    def __init__(self, *, fetchones=None, fetchalls=None):
        self.cursor = FakeCursor(fetchones=fetchones, fetchalls=fetchalls)

    @contextmanager
    def transaction(self):
        yield FakeConnection(self.cursor)
