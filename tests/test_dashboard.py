from __future__ import annotations

from app import create_app
from app.security.tokens import create_access_token
from app.utils.errors import ApiError


class TestConfig:
    SECRET_KEY = "test-secret-key-with-at-least-32-bytes"
    TESTING = True
    LOG_LEVEL = "CRITICAL"


def make_app():
    app = create_app(TestConfig)
    app.config.update(TESTING=True)
    return app


def login(client, app, user_id="11111111-1111-1111-1111-111111111111"):
    with app.app_context():
        token = create_access_token(user_id)
    with client.session_transaction() as session:
        session["access_token"] = token
    return user_id


def project():
    return {
        "projectID": "22222222-2222-2222-2222-222222222222",
        "name": "Personal Models",
        "slug": "personal-models",
        "k8s_namespace": "miniten-personal-models",
        "created_at": "2026-01-01T00:00:00Z",
        "role": "owner",
    }


def model(**overrides):
    row = {
        "modelDeploymentID": "33333333-3333-3333-3333-333333333333",
        "projectID": project()["projectID"],
        "name": "qwen",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "status": "running",
        "replicas": 1,
        "desired_generation": 1,
        "resources": {
            "cpu_request": "1",
            "cpu_limit": "2",
            "memory_request": "4Gi",
            "memory_limit": "8Gi",
            "gpu_count": 0,
        },
        "vllm": {
            "image": "vllm/vllm-openai-cpu:latest-x86_64",
            "dtype": "auto",
            "max_model_len": 1024,
        },
        "autoscaling": {
            "enabled": True,
            "min_replicas": 1,
            "max_replicas": 2,
            "target_cpu_utilization": 70,
        },
    }
    row.update(overrides)
    return row


def overview():
    return {
        "projectID": project()["projectID"],
        "summary": {
            "total_models": 1,
            "running_models": 1,
            "stopped_models": 0,
            "total_requests": 3,
            "error_count": 0,
            "average_latency_ms": 42,
            "last_request_at": "2026-01-01T00:00:00Z",
        },
        "models": [],
    }


def patch_project_detail(monkeypatch):
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.get_project",
        lambda user_id, project_id: project(),
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.list_model_deployments",
        lambda user_id, project_id: {"modelDeployments": [model()]},
    )
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.list_project_members",
        lambda user_id, project_id: {
            "members": [
                {
                    "userID": user_id,
                    "email": "user@example.com",
                    "role": "owner",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ]
        },
    )
    monkeypatch.setattr(
        "app.routes.dashboard.api_key_service.list_api_keys",
        lambda user_id, project_id: {
            "api_keys": [
                {
                    "apiKeyID": "44444444-4444-4444-4444-444444444444",
                    "projectID": project_id,
                    "name": "local",
                    "key_prefix": "mt_live",
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_used_at": None,
                    "revoked_at": None,
                },
                {
                    "apiKeyID": "55555555-5555-5555-5555-555555555555",
                    "projectID": project_id,
                    "name": "old",
                    "key_prefix": "mt_live",
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_used_at": None,
                    "revoked_at": "2026-01-02T00:00:00Z",
                }
            ]
        },
    )
    monkeypatch.setattr(
        "app.routes.dashboard.analytics_service.get_project_overview",
        lambda user_id, project_id: overview(),
    )


def test_dashboard_index_renders_public_entry():
    app = make_app()
    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"MiniTen" in response.data
    assert b"Create account" in response.data


def test_dashboard_projects_requires_login():
    app = make_app()
    response = app.test_client().get("/projects")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_flash_messages_can_be_dismissed(monkeypatch):
    app = make_app()
    client = app.test_client()

    def fail_login(email, password):
        raise ApiError("invalid_credentials", "Invalid email or password.", 401)

    monkeypatch.setattr("app.routes.dashboard.auth_service.login", fail_login)

    response = client.post(
        "/login",
        data={"email": "user@example.com", "password": "bad-password"},
    )

    assert response.status_code == 200
    assert b"data-dismiss-message" in response.data
    assert b"Dismiss message" in response.data


def test_projects_page_lists_projects(monkeypatch):
    app = make_app()
    client = app.test_client()
    user_id = login(client, app)
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.list_projects",
        lambda current_user_id: {"projects": [project()]},
    )
    monkeypatch.setattr(
        "app.routes.dashboard.user_service.get_user",
        lambda current_user_id: {
            "userID": user_id,
            "email": "user@example.com",
            "created_at": "2026-01-01T00:00:00Z",
            "last_login_at": None,
        },
    )

    response = client.get("/projects")

    assert response.status_code == 200
    assert b"Personal Models" in response.data
    assert b"miniten-logo.png" in response.data
    assert b'aria-label="MiniTen home"' in response.data
    assert b"Delete account" in response.data
    assert b'data-confirm="Delete your MiniTen account? This cannot be undone."' in response.data


def test_login_preserves_email_after_failed_attempt(monkeypatch):
    app = make_app()
    client = app.test_client()

    def fail_login(email, password):
        raise ApiError("invalid_credentials", "Invalid email or password.", 401)

    monkeypatch.setattr("app.routes.dashboard.auth_service.login", fail_login)

    response = client.post(
        "/login",
        data={"email": "user@example.com", "password": "bad-password"},
    )

    assert response.status_code == 200
    assert b'value="user@example.com"' in response.data


def test_stale_dashboard_session_redirects_to_login(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)

    def missing_user(current_user_id):
        raise ApiError("user_not_found", "User not found.", 404)

    monkeypatch.setattr("app.routes.dashboard.user_service.get_user", missing_user)
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.list_projects",
        lambda current_user_id: {"projects": []},
    )

    response = client.get("/projects")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    with client.session_transaction() as session:
        assert "access_token" not in session


def test_dashboard_api_error_renders_error_page(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)

    def fail_list(current_user_id):
        raise ApiError("project_lookup_failed", "Project lookup failed.", 404)

    monkeypatch.setattr("app.routes.dashboard.project_service.list_projects", fail_list)

    response = client.get("/projects")

    assert response.status_code == 404
    assert b"Something went wrong" in response.data
    assert b"Project lookup failed." in response.data
    assert b"Projects" in response.data


def test_dashboard_missing_route_renders_error_page():
    app = make_app()
    response = app.test_client().get("/missing-dashboard-page")

    assert response.status_code == 404
    assert b"Something went wrong" in response.data
    assert b"Log in" in response.data


def test_project_detail_aligns_with_cli_command_groups(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    patch_project_detail(monkeypatch)

    response = client.get(f"/projects/{project()['projectID']}")

    assert response.status_code == 200
    assert b"Deploy model" in response.data
    assert b"Delete project Personal Models?" in response.data
    assert b"data-confirm" in response.data
    assert b"API Keys" in response.data
    assert b"local" in response.data
    assert b"old" in response.data
    assert b"status-active" in response.data
    assert b"Active" in response.data
    assert b"status-revoked" in response.data
    assert b"Revoked" in response.data
    assert b"Members" in response.data
    assert b"Analytics" in response.data


def test_model_new_uses_low_memory_defaults(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.get_project",
        lambda user_id, project_id: project(),
    )

    response = client.get(f"/projects/{project()['projectID']}/models/new")

    assert response.status_code == 200
    assert b'name="cpu_request" placeholder="1" value="1"' in response.data
    assert b'name="cpu_limit" placeholder="4" value="4"' in response.data
    assert b'name="memory_request" placeholder="1Gi" value="1Gi"' in response.data
    assert b'name="memory_limit" placeholder="6Gi" value="6Gi"' in response.data
    assert b'name="max_model_len" type="number" min="1" value="256"' in response.data
    assert b'<option value="false" selected>false</option>' in response.data
    assert b'value="">default</option>' not in response.data
    assert b"data-autoscaling-toggle" in response.data
    assert b"data-autoscaling-field" in response.data
    assert b'name="min_replicas" type="number" min="1" value=""' in response.data
    assert b'data-autoscaling-default="1" disabled' in response.data


def test_model_detail_delete_requires_confirmation_and_failed_retry(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.get_project",
        lambda user_id, project_id: project(),
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.get_model_deployment",
        lambda user_id, project_id, model_id: model(status="failed"),
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.list_model_deployment_jobs",
        lambda user_id, project_id, model_id: {"deploymentJobs": []},
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.get_model_deployment_status",
        lambda user_id, project_id, model_id: {
            "kubernetes": {
                "available": True,
                "reason": None,
                "readiness": {},
                "recentLogs": [],
            }
        },
    )

    response = client.get(
        f"/projects/{project()['projectID']}/models/{model()['modelDeploymentID']}"
    )

    assert response.status_code == 200
    assert b"Delete model qwen?" in response.data
    assert b"data-confirm" in response.data
    assert b"Retry" in response.data
    assert b"Back to project" in response.data
    assert f'href="/projects/{project()["projectID"]}"'.encode() in response.data
    assert b"data-auto-sync-url" in response.data
    assert b'data-auto-sync-interval-ms="120000"' in response.data
    assert b"Refresh jobs and status" in response.data
    assert b"&#8635;" in response.data


def test_api_key_created_page_has_copy_button(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)

    monkeypatch.setattr(
        "app.routes.dashboard.api_key_service.create_api_key",
        lambda user_id, project_id, name: {"api_key": "mt_live_example_secret"},
    )

    response = client.post(
        f"/projects/{project()['projectID']}/api-keys",
        data={"name": "local"},
    )

    assert response.status_code == 200
    assert b'id="created-api-key"' in response.data
    assert b'data-copy-target="#created-api-key"' in response.data


def test_api_key_revoke_missing_is_idempotent(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)

    def missing_key(user_id, project_id, api_key_id):
        raise ApiError("api_key_not_found", "API key not found.", 404)

    monkeypatch.setattr(
        "app.routes.dashboard.api_key_service.revoke_api_key",
        missing_key,
    )

    response = client.post(
        f"/projects/{project()['projectID']}/api-keys/44444444-4444-4444-4444-444444444444/revoke",
    )

    assert response.status_code == 302
    with client.session_transaction() as session:
        assert ("warning", "API key was already removed.") in session["_flashes"]


def test_model_scale_form_calls_service(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    calls = []

    def fake_scale(user_id, project_id, model_id, replicas):
        calls.append(
            {
                "project_id": project_id,
                "model_id": model_id,
                "replicas": replicas,
            }
        )
        return {}

    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.scale_model_deployment",
        fake_scale,
    )

    response = client.post(
        f"/projects/{project()['projectID']}/models/{model()['modelDeploymentID']}/scale",
        data={"replicas": "2"},
    )

    assert response.status_code == 302
    assert calls == [
        {
            "project_id": project()["projectID"],
            "model_id": model()["modelDeploymentID"],
            "replicas": "2",
        }
    ]


def test_model_retry_command_uses_start_service(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    calls = []

    def fake_start(user_id, project_id, model_id):
        calls.append({"project_id": project_id, "model_id": model_id})
        return {}

    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.start_model_deployment",
        fake_start,
    )

    response = client.post(
        f"/projects/{project()['projectID']}/models/{model()['modelDeploymentID']}/retry",
    )

    assert response.status_code == 302
    assert calls == [
        {
            "project_id": project()["projectID"],
            "model_id": model()["modelDeploymentID"],
        }
    ]


def test_model_sync_fetch_is_quiet_background_response(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    calls = []

    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.sync_model_deployment_status",
        lambda user_id, project_id, model_id: calls.append(model_id),
    )

    response = client.post(
        f"/projects/{project()['projectID']}/models/{model()['modelDeploymentID']}/sync",
        headers={"X-Requested-With": "fetch"},
    )

    assert response.status_code == 204
    assert calls == [model()["modelDeploymentID"]]


def test_inference_page_posts_chat_completion(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    calls = []

    def fake_chat(api_key, body):
        calls.append({"api_key": api_key, "body": body})
        return {"choices": [{"message": {"content": "hello"}}]}, 200

    monkeypatch.setattr(
        "app.routes.dashboard.inference_service.chat_completions",
        fake_chat,
    )

    response = client.post(
        "/inference",
        data={
            "api_key": "mt_test",
            "model": "qwen",
            "prompt": "Say hello",
            "max_tokens": "8",
            "temperature": "0",
        },
    )

    assert response.status_code == 200
    assert calls[0]["api_key"] == "mt_test"
    assert calls[0]["body"]["model"] == "qwen"
    assert calls[0]["body"]["messages"][0]["content"] == "Say hello"
    assert b'name="api_key" type="text" value="mt_test"' in response.data
    assert b'name="model" value="qwen"' in response.data
    assert b"Say hello</textarea>" in response.data
    assert b'autocomplete="off"' in response.data
    assert b'type="password"' not in response.data
