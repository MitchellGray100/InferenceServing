"""Inference route and service tests."""

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
import requests

from app import create_app
from app.routes import inference
from app.services import inference_service
from app.utils.errors import ApiError


PROJECT_ID = "a2fc41b7-862e-4060-b466-2376f29227bb"
API_KEY_ID = "4c659341-7357-42f6-9ee8-801a1f340b35"
MODEL_DEPLOYMENT_ID = "bf3dc090-5bb4-46f6-964d-6cd8375ddf56"
NOW = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)


class TestConfig:
    TESTING = True
    SECRET_KEY = "test-secret-key-change-me-32-bytes"
    API_KEY_HASH_SECRET = "test-api-key-hash-secret-change-me-32-bytes"
    INFERENCE_UPSTREAM_TIMEOUT_SECONDS = 12
    API_DEBUG = False


@pytest.fixture
def app():
    return create_app(TestConfig)


@pytest.fixture
def client(app):
    return app.test_client()


def deployment_row(**overrides):
    row = {
        "model_deployment_id": MODEL_DEPLOYMENT_ID,
        "project_id": PROJECT_ID,
        "name": "qwen-small-prod",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "status": "running",
        "k8s_namespace": "miniten-personal",
        "k8s_service_name": "qwen-small-prod",
        "created_at": NOW,
    }
    row.update(overrides)
    return row


def test_chat_completions_route(monkeypatch, client) -> None:
    def chat_completions(raw_api_key, body):
        assert raw_api_key == "mt_live_visible_secret"
        assert body["model"] == "qwen-small-prod"
        return {"id": "chatcmpl_123", "object": "chat.completion"}, 200

    monkeypatch.setattr(
        "app.routes.inference.inference_service.chat_completions",
        chat_completions,
    )

    response = client.post(
        "/v1/chat/completions",
        json={"model": "qwen-small-prod", "messages": []},
        headers={"Authorization": "Bearer mt_live_visible_secret"},
    )

    assert response.status_code == 200
    assert response.get_json()["id"] == "chatcmpl_123"


def test_chat_completions_route_streams(monkeypatch, client) -> None:
    def chat_completions_stream(raw_api_key, body):
        assert raw_api_key == "mt_live_visible_secret"
        assert body["stream"] is True
        return iter([b"data: first\n\n", b"data: [DONE]\n\n"]), 200

    monkeypatch.setattr(
        "app.routes.inference.inference_service.chat_completions_stream",
        chat_completions_stream,
    )

    response = client.post(
        "/v1/chat/completions",
        json={"model": "qwen-small-prod", "messages": [], "stream": True},
        headers={"Authorization": "Bearer mt_live_visible_secret"},
    )

    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert response.data == b"data: first\n\ndata: [DONE]\n\n"


def test_list_models_route(monkeypatch, client) -> None:
    expected = {"object": "list", "data": []}
    monkeypatch.setattr(
        "app.routes.inference.inference_service.list_models",
        lambda raw_api_key: expected,
    )

    response = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer mt_live_visible_secret"},
    )

    assert response.status_code == 200
    assert response.get_json() == expected


def test_inference_routes_require_project_api_key(client) -> None:
    response = client.get("/v1/models")

    assert response.status_code == 401
    assert response.get_json()["error"]["type"] == "unauthorized"


def test_get_project_api_key_accepts_case_insensitive_bearer(app) -> None:
    with app.test_request_context(headers={"Authorization": "bEaReR raw-key"}):
        assert inference.get_project_api_key() == "raw-key"


def test_build_vllm_url() -> None:
    app = create_app(TestConfig)
    with app.app_context():
        assert inference_service.build_vllm_url(
            deployment_row(), "/v1/chat/completions"
        ) == (
            "http://qwen-small-prod.miniten-personal.svc.cluster.local:8000"
            "/v1/chat/completions"
        )
        assert inference_service.build_vllm_url(deployment_row(), "health").endswith(
            ":8000/health"
        )


def test_build_vllm_url_uses_explicit_base_url() -> None:
    app = create_app(TestConfig)
    with app.app_context():
        assert inference_service.build_vllm_url(
            deployment_row(),
            "/v1/chat/completions",
            base_url="http://127.0.0.1:51234",
        ) == "http://127.0.0.1:51234/v1/chat/completions"


def test_upstream_request_failed_message_includes_local_port_forward_hint() -> None:
    class DebugConfig(TestConfig):
        API_DEBUG = True

    app = create_app(DebugConfig)
    with app.app_context():
        message = inference_service.upstream_request_failed_message(deployment_row())

    assert "kubectl port-forward" in message
    assert "svc/qwen-small-prod" in message
    assert "miniten-personal" in message


def test_maybe_local_port_forward_starts_kubectl_when_needed(monkeypatch, app) -> None:
    calls = []

    class FakeProcess:
        stdout = None

        def poll(self):
            return None

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout):
            calls.append(("wait", timeout))

    def fake_popen(args, stdout, stderr, text):
        calls.append(args)
        return FakeProcess()

    monkeypatch.setattr(inference_service, "find_free_local_port", lambda: 51234)
    port_checks = iter([True])
    monkeypatch.setattr(inference_service, "is_local_port_open", lambda port: next(port_checks))
    monkeypatch.setattr(inference_service.subprocess, "Popen", fake_popen)

    class DebugConfig(TestConfig):
        API_DEBUG = True

    debug_app = create_app(DebugConfig)
    with debug_app.app_context():
        with inference_service.maybe_local_port_forward(deployment_row()) as port:
            assert port == 51234
            calls.append("inside")

    assert calls[0][:3] == ["kubectl", "port-forward", "-n"]
    assert "service/qwen-small-prod" in calls[0]
    assert "51234:8000" in calls[0]
    assert "inside" in calls
    assert "terminate" in calls


def test_maybe_local_port_forward_does_not_reuse_existing_fixed_forward(monkeypatch) -> None:
    class DebugConfig(TestConfig):
        API_DEBUG = True

    debug_app = create_app(DebugConfig)
    calls = []

    class FakeProcess:
        stdout = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout):
            pass

    monkeypatch.setattr(inference_service, "find_free_local_port", lambda: 51235)
    monkeypatch.setattr(inference_service, "is_local_port_open", lambda port: True)
    monkeypatch.setattr(
        inference_service.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append(args) or FakeProcess(),
    )

    with debug_app.app_context():
        with inference_service.maybe_local_port_forward(deployment_row()) as port:
            assert port == 51235
            pass

    assert calls
    assert "51235:8000" in calls[0]


def test_vllm_request_url_uses_configured_forward_url() -> None:
    class ForwardConfig(TestConfig):
        INFERENCE_LOCAL_PORT_FORWARD_URL = "http://127.0.0.1:18080"

    forward_app = create_app(ForwardConfig)
    with forward_app.app_context():
        with inference_service.vllm_request_url(
            deployment_row(),
            "/v1/chat/completions",
        ) as url:
            assert url == "http://127.0.0.1:18080/v1/chat/completions"


def test_ensure_deployment_running_rejects_non_running() -> None:
    with pytest.raises(ApiError) as error:
        inference_service.ensure_deployment_running(deployment_row(status="deploying"))

    assert error.value.type == "model_not_ready"


def test_get_deployment_for_inference_success_and_not_found(monkeypatch) -> None:
    fake = FakeTransaction(fetchones=[deployment_row(), None])
    monkeypatch.setattr(inference_service, "transaction", fake.transaction)

    row = inference_service.get_deployment_for_inference(PROJECT_ID, "qwen-small-prod")
    assert row["name"] == "qwen-small-prod"

    with pytest.raises(ApiError) as error:
        inference_service.get_deployment_for_inference(PROJECT_ID, "missing")

    assert error.value.type == "model_not_found"


def test_list_models(monkeypatch) -> None:
    fake = FakeTransaction(fetchalls=[[deployment_row()]])
    monkeypatch.setattr(inference_service, "transaction", fake.transaction)
    monkeypatch.setattr(
        inference_service.api_key_service,
        "authenticate_project_api_key",
        lambda raw_key: {"apiKeyID": API_KEY_ID, "projectID": PROJECT_ID},
    )

    response = inference_service.list_models("raw-key")

    assert response["object"] == "list"
    assert response["data"][0]["id"] == "qwen-small-prod"


def test_serialize_openai_model_handles_missing_datetime_timestamp() -> None:
    response = inference_service.serialize_openai_model(
        {"name": "qwen-small-prod", "created_at": "not-a-datetime"}
    )

    assert response["created"] == 0
    assert response["owned_by"] == "miniten"


def test_parse_upstream_json_requires_object() -> None:
    assert inference_service.parse_upstream_json(FakeResponse({"ok": True})) == {"ok": True}

    with pytest.raises(ValueError):
        inference_service.parse_upstream_json(FakeResponse(["not", "object"]))


def test_record_inference_request(monkeypatch) -> None:
    fake = FakeTransaction()
    monkeypatch.setattr(inference_service, "transaction", fake.transaction)

    inference_service.record_inference_request(
        project_id=PROJECT_ID,
        model_deployment_id=MODEL_DEPLOYMENT_ID,
        api_key_id=API_KEY_ID,
        api_key_prefix="mt_live_visible",
        status_code=200,
        latency_ms=10,
        error_type=None,
        request_path="/v1/chat/completions",
        method="POST",
        streamed=False,
    )

    assert fake.cursor.executed[0]["status_code"] == 200
    assert fake.cursor.executed[0]["streamed"] is False
    assert fake.cursor.executed[0]["api_key_prefix"] == "mt_live_visible"


def test_chat_completions_success_logs_request(monkeypatch, app) -> None:
    records = []
    monkeypatch.setattr(
        inference_service.api_key_service,
        "authenticate_project_api_key",
        lambda raw_key: {"apiKeyID": API_KEY_ID, "projectID": PROJECT_ID},
    )
    monkeypatch.setattr(
        inference_service,
        "get_deployment_for_inference",
        lambda project_id, model_name: deployment_row(),
    )
    monkeypatch.setattr(
        inference_service.requests,
        "post",
        lambda url, json, timeout: FakeResponse({"id": "chatcmpl_123"}, 200),
    )
    monkeypatch.setattr(
        inference_service,
        "record_inference_request",
        lambda **kwargs: records.append(kwargs),
    )

    with app.app_context():
        body, status = inference_service.chat_completions(
            "raw-key",
            {"model": "qwen-small-prod", "messages": []},
        )

    assert status == 200
    assert body == {"id": "chatcmpl_123"}
    assert records[0]["status_code"] == 200
    assert records[0]["error_type"] is None
    assert "api_key_prefix" in records[0]


def test_chat_completions_records_upstream_http_errors(monkeypatch, app) -> None:
    records = []
    monkeypatch.setattr(
        inference_service.api_key_service,
        "authenticate_project_api_key",
        lambda raw_key: {"apiKeyID": API_KEY_ID, "projectID": PROJECT_ID},
    )
    monkeypatch.setattr(
        inference_service,
        "get_deployment_for_inference",
        lambda project_id, model_name: deployment_row(),
    )
    monkeypatch.setattr(
        inference_service.requests,
        "post",
        lambda url, json, timeout: FakeResponse({"error": {"message": "bad"}}, 400),
    )
    monkeypatch.setattr(
        inference_service,
        "record_inference_request",
        lambda **kwargs: records.append(kwargs),
    )

    with app.app_context():
        body, status = inference_service.chat_completions(
            "raw-key",
            {"model": "qwen-small-prod", "messages": []},
        )

    assert status == 400
    assert body["error"]["message"] == "bad"
    assert records[0]["error_type"] == "upstream_4xx"


def test_chat_completions_stream_proxies_chunks_and_logs_request(monkeypatch, app) -> None:
    records = []
    monkeypatch.setattr(
        inference_service.api_key_service,
        "authenticate_project_api_key",
        lambda raw_key: {
            "apiKeyID": API_KEY_ID,
            "projectID": PROJECT_ID,
            "apiKeyPrefix": "mt_live_visible",
        },
    )
    monkeypatch.setattr(
        inference_service,
        "get_deployment_for_inference",
        lambda project_id, model_name: deployment_row(),
    )
    monkeypatch.setattr(
        inference_service.requests,
        "post",
        lambda url, json, timeout, stream: FakeStreamingResponse(
            [b"data: one\n\n", b"data: [DONE]\n\n"],
            200,
        ),
    )
    monkeypatch.setattr(
        inference_service,
        "record_inference_request",
        lambda **kwargs: records.append(kwargs),
    )

    with app.app_context():
        chunks, status = inference_service.chat_completions_stream(
            "raw-key",
            {"model": "qwen-small-prod", "messages": [], "stream": True},
        )
        assert status == 200
        assert b"".join(chunks) == b"data: one\n\ndata: [DONE]\n\n"

    assert records[0]["streamed"] is True
    assert records[0]["status_code"] == 200
    assert records[0]["api_key_prefix"] == "mt_live_visible"


@pytest.mark.parametrize(
    ("exception", "error_type", "status_code"),
    [
        (requests.Timeout("slow"), "upstream_timeout", 504),
        (requests.RequestException("network"), "upstream_error", 502),
        (ValueError("bad json"), "upstream_invalid_response", 502),
    ],
)
def test_chat_completions_upstream_errors_log_request(
    monkeypatch,
    app,
    exception,
    error_type,
    status_code,
) -> None:
    records = []
    monkeypatch.setattr(
        inference_service.api_key_service,
        "authenticate_project_api_key",
        lambda raw_key: {"apiKeyID": API_KEY_ID, "projectID": PROJECT_ID},
    )
    monkeypatch.setattr(
        inference_service,
        "get_deployment_for_inference",
        lambda project_id, model_name: deployment_row(),
    )

    def post(url, json, timeout):
        raise exception

    monkeypatch.setattr(inference_service.requests, "post", post)
    monkeypatch.setattr(
        inference_service,
        "record_inference_request",
        lambda **kwargs: records.append(kwargs),
    )

    with app.app_context(), pytest.raises(ApiError) as error:
        inference_service.chat_completions(
            "raw-key",
            {"model": "qwen-small-prod", "messages": []},
        )

    assert error.value.type == error_type
    assert error.value.status_code == status_code
    assert records[0]["status_code"] == status_code
    assert records[0]["error_type"] == error_type


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeStreamingResponse:
    def __init__(self, chunks, status_code=200):
        self.chunks = chunks
        self.status_code = status_code
        self.closed = False

    def iter_content(self, chunk_size=None):
        yield from self.chunks

    def close(self):
        self.closed = True


class FakeCursor:
    def __init__(self, *, fetchones=None, fetchalls=None):
        self.fetchones = list(fetchones or [])
        self.fetchalls = list(fetchalls or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(params)

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
