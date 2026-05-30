from __future__ import annotations

from pathlib import Path

import pytest

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
    assert b'rel="icon" type="image/png"' in response.data
    assert b"favicon.png" in response.data
    assert b"miniten-logo.png" in response.data
    assert b'class="public-logo"' in response.data


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


def test_login_page_shows_miniten_logo():
    app = make_app()
    response = app.test_client().get("/login")

    assert response.status_code == 200
    assert b"miniten-logo.png" in response.data
    assert b'class="auth-logo"' in response.data


def test_projects_page_lists_projects(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    with client.session_transaction() as session:
        session["user_email"] = "user@example.com"
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.list_projects",
        lambda current_user_id: {"projects": [project()]},
    )

    response = client.get("/projects")

    assert response.status_code == 200
    assert b"Personal Models" in response.data
    assert b"miniten-logo.png" in response.data
    assert b'aria-label="MiniTen home"' in response.data
    assert b"Account" in response.data
    assert b"user@example.com" in response.data
    assert b"Delete account" not in response.data


def test_dashboard_login_stores_user_email(monkeypatch):
    app = make_app()
    client = app.test_client()

    with app.app_context():
        token = create_access_token("11111111-1111-1111-1111-111111111111")

    monkeypatch.setattr(
        "app.routes.dashboard.auth_service.login",
        lambda email, password: {
            "access_token": token,
            "user": {"email": "user@example.com"},
        },
    )

    response = client.post(
        "/login",
        data={"email": "user@example.com", "password": "password123"},
    )

    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["user_email"] == "user@example.com"


def test_account_page_shows_account_controls(monkeypatch):
    app = make_app()
    client = app.test_client()
    user_id = login(client, app)
    monkeypatch.setattr(
        "app.routes.dashboard.user_service.get_user",
        lambda current_user_id: {
            "userID": user_id,
            "email": "user@example.com",
            "created_at": "2026-01-01T00:00:00Z",
            "last_login_at": None,
        },
    )

    response = client.get("/account")

    assert response.status_code == 200
    assert b"user@example.com" in response.data
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

    response = client.get("/account")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    with client.session_transaction() as session:
        assert "access_token" not in session


@pytest.mark.real_auth_user_check
def test_deleted_user_dashboard_session_is_cleared(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)

    def missing_user_from_token(user_id, token_version=0):
        raise ApiError("unauthorized", "Missing or invalid access token.", 401)

    monkeypatch.setattr(
        "app.routes.dashboard.require_existing_user_id",
        missing_user_from_token,
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
    assert b"Refresh models" in response.data
    assert b"Refresh API keys" in response.data
    assert b"Refresh members" in response.data
    assert b"/models/sync" not in response.data
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
    assert b"Fixed replicas" in response.data
    assert (
        b'name="replicas" type="number" min="0" value="1" data-fixed-replica-field '
        b'data-fixed-replica-default="1" >'
    ) in response.data
    assert b"data-autoscaling-toggle" in response.data
    assert b"data-autoscaling-field" in response.data
    assert b'name="min_replicas" type="number" min="1" value=""' in response.data
    assert b'data-autoscaling-default="1" disabled' in response.data


def test_model_new_validation_error_stays_on_form_with_values(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.get_project",
        lambda user_id, project_id: project(),
    )

    def reject_create(user_id, project_id, data):
        raise ApiError("validation_error", "CPU request is invalid.", 400)

    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.create_model_deployment",
        reject_create,
    )

    response = client.post(
        f"/projects/{project()['projectID']}/models/new",
        data={
            "name": "small-llm",
            "model_id": "HuggingFaceTB/SmolLM2-135M-Instruct",
            "replicas": "2",
            "cpu_request": "bad-cpu",
            "cpu_limit": "4",
            "memory_request": "1Gi",
            "memory_limit": "6Gi",
            "gpu_count": "0",
            "dtype": "auto",
            "max_model_len": "256",
            "autoscaling_enabled": "false",
        },
    )

    assert response.status_code == 400
    assert b"Deploy model" in response.data
    assert b"CPU request is invalid." in response.data
    assert b'value="small-llm"' in response.data
    assert b'value="HuggingFaceTB/SmolLM2-135M-Instruct"' in response.data
    assert b'value="bad-cpu"' in response.data
    assert response.headers.get("Location") is None


def test_model_new_autoscaling_disables_fixed_replicas_after_validation_error(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.get_project",
        lambda user_id, project_id: project(),
    )

    def reject_create(user_id, project_id, data):
        raise ApiError("validation_error", "Model ID is invalid.", 400)

    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.create_model_deployment",
        reject_create,
    )

    response = client.post(
        f"/projects/{project()['projectID']}/models/new",
        data={
            "name": "small-llm",
            "model_id": "bad-model",
            "cpu_request": "1",
            "cpu_limit": "4",
            "memory_request": "1Gi",
            "memory_limit": "6Gi",
            "gpu_count": "0",
            "dtype": "auto",
            "max_model_len": "256",
            "autoscaling_enabled": "true",
            "min_replicas": "2",
            "max_replicas": "4",
            "target_cpu_utilization": "70",
        },
    )

    assert response.status_code == 400
    assert b'<option value="true" selected>true</option>' in response.data
    assert (
        b'name="replicas" type="number" min="0" value="1" data-fixed-replica-field '
        b'data-fixed-replica-default="1" disabled'
    ) in response.data


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
    assert b'class="live-status-box"' in response.data
    assert b"data-auto-sync-url" in response.data
    assert b'data-auto-sync-interval-ms="120000"' in response.data
    assert b"Refresh jobs and status" in response.data
    assert b"&#8635;" in response.data
    assert b'aria-label="Replicas"' not in response.data
    assert b"Scale</button>" not in response.data
    assert b"Disable autoscaling before manually scaling replicas." not in response.data


def test_model_detail_populates_current_deployment_settings(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    current_model = model(
        replicas=2,
        resources={
            "cpu_request": "750m",
            "cpu_limit": "3",
            "memory_request": "2Gi",
            "memory_limit": "5Gi",
            "gpu_count": 1,
        },
        vllm={
            "image": "vllm/vllm-openai:latest",
            "dtype": "float16",
            "max_model_len": 768,
        },
        autoscaling={
            "enabled": True,
            "min_replicas": 2,
            "max_replicas": 4,
            "target_cpu_utilization": 65,
        },
    )
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.get_project",
        lambda user_id, project_id: project(),
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.get_model_deployment",
        lambda user_id, project_id, model_id: current_model,
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.list_model_deployment_jobs",
        lambda user_id, project_id, model_id: {"deploymentJobs": []},
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.get_model_deployment_status",
        lambda user_id, project_id, model_id: {"kubernetes": {}},
    )

    response = client.get(
        f"/projects/{project()['projectID']}/models/{model()['modelDeploymentID']}"
    )

    assert response.status_code == 200
    assert b'aria-label="Replicas"' not in response.data
    assert (
        b'name="replicas" type="number" min="0" value="2" data-fixed-replica-field '
        b'data-fixed-replica-default="1" disabled'
    ) in response.data
    assert b'name="cpu_request" placeholder="1" value="750m"' in response.data
    assert b'name="cpu_limit" placeholder="4" value="3"' in response.data
    assert b'name="memory_request" placeholder="1Gi" value="2Gi"' in response.data
    assert b'name="memory_limit" placeholder="6Gi" value="5Gi"' in response.data
    assert b'name="gpu_count" type="number" min="0" value="1"' in response.data
    assert b'<option value="float16" selected>float16</option>' in response.data
    assert b'name="max_model_len" type="number" min="1" value="768"' in response.data
    assert b'<option value="true" selected>true</option>' in response.data
    assert b'name="min_replicas" type="number" min="1" value="2"' in response.data
    assert b'name="max_replicas" type="number" min="1" value="4"' in response.data
    assert b'name="target_cpu_utilization" type="number" min="1" max="100" value="65"' in response.data


def test_model_detail_fixed_replicas_enabled_when_autoscaling_disabled(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    current_model = model(
        replicas=3,
        autoscaling={
            "enabled": False,
            "min_replicas": None,
            "max_replicas": None,
            "target_cpu_utilization": None,
        },
    )
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.get_project",
        lambda user_id, project_id: project(),
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.get_model_deployment",
        lambda user_id, project_id, model_id: current_model,
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.list_model_deployment_jobs",
        lambda user_id, project_id, model_id: {"deploymentJobs": []},
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.get_model_deployment_status",
        lambda user_id, project_id, model_id: {"kubernetes": {}},
    )

    response = client.get(
        f"/projects/{project()['projectID']}/models/{model()['modelDeploymentID']}"
    )

    assert response.status_code == 200
    assert (
        b'name="replicas" type="number" min="0" value="3" data-fixed-replica-field '
        b'data-fixed-replica-default="1" >'
    ) in response.data


def test_autoscaling_script_preserves_server_rendered_values():
    script = (Path(__file__).resolve().parents[1] / "app" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "if (!field.value)" in script
    assert "field.dataset.autoscalingValue || field.dataset.autoscalingDefault" in script
    assert "data-fixed-replica-field" in script
    assert "field.disabled = enabled" in script
    assert "field.dataset.fixedReplicaValue || field.dataset.fixedReplicaDefault" in script


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
            "replicas": 2,
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


def test_model_hard_restart_requires_confirmation_and_calls_service(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    calls = []

    monkeypatch.setattr(
        "app.routes.dashboard.project_service.get_project",
        lambda user_id, project_id: project(),
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.get_model_deployment",
        lambda user_id, project_id, model_id: model(),
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.list_model_deployment_jobs",
        lambda user_id, project_id, model_id: {"deploymentJobs": []},
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.get_model_deployment_status",
        lambda user_id, project_id, model_id: {"kubernetes": {}},
    )

    response = client.get(
        f"/projects/{project()['projectID']}/models/{model()['modelDeploymentID']}"
    )

    assert response.status_code == 200
    assert b"Hard restart" in response.data
    assert b"force deletes Kubernetes runtime resources" in response.data

    def fake_hard_restart(user_id, project_id, model_id):
        calls.append({"project_id": project_id, "model_id": model_id})
        return {}

    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.hard_restart_model_deployment",
        fake_hard_restart,
    )

    response = client.post(
        f"/projects/{project()['projectID']}/models/{model()['modelDeploymentID']}/hard-restart",
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


def test_model_analytics_has_back_to_project_button(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.get_project",
        lambda user_id, project_id: project(),
    )
    monkeypatch.setattr(
        "app.routes.dashboard.analytics_service.get_model_metrics",
        lambda user_id, project_id, model_name: {
            "metrics": {
                "request_count": 0,
                "success_count": 0,
                "error_count": 0,
                "average_latency_ms": None,
                "p95_latency_ms": None,
                "last_request_at": None,
            }
        },
    )
    monkeypatch.setattr(
        "app.routes.dashboard.analytics_service.list_model_requests",
        lambda user_id, project_id, model_name, **filters: {
            "requests": [
                {
                    "inferenceRequestID": "77777777-7777-7777-7777-777777777777",
                    "projectID": project_id,
                    "modelDeploymentID": model()["modelDeploymentID"],
                    "apiKeyID": "44444444-4444-4444-4444-444444444444",
                    "apiKeyName": "local",
                    "apiKeyPrefix": "mt_live",
                    "status_code": 200,
                    "latency_ms": 42,
                    "error_type": None,
                    "request_path": "/v1/chat/completions",
                    "method": "POST",
                    "streamed": False,
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ]
        },
    )
    monkeypatch.setattr(
        "app.routes.dashboard.analytics_service.list_model_events",
        lambda user_id, project_id, model_name: {"events": []},
    )

    response = client.get(f"/projects/{project()['projectID']}/analytics/qwen")

    assert response.status_code == 200
    assert b"Back to project" in response.data
    assert f'href="/projects/{project()["projectID"]}"'.encode() in response.data
    assert b"analytics-stats" in response.data
    assert b"table-scroll" in response.data
    assert b"analytics-scroll" in response.data
    assert b"P95 latency" in response.data
    assert b"Last request" in response.data
    assert b"API key" in response.data
    assert b"local" not in response.data
    assert b"mt_live" in response.data
    assert b"POST /v1/chat/completions" in response.data


def test_model_logs_has_back_to_model_button(monkeypatch):
    app = make_app()
    client = app.test_client()
    login(client, app)
    monkeypatch.setattr(
        "app.routes.dashboard.project_service.get_project",
        lambda user_id, project_id: project(),
    )
    monkeypatch.setattr(
        "app.routes.dashboard.model_deployment_service.list_model_logs",
        lambda user_id, project_id, model_name, tail: {
            "model": model(),
            "logs": [{"pod": "qwen-pod", "text": "ready"}],
        },
    )

    response = client.get(f"/projects/{project()['projectID']}/models/qwen/logs")

    assert response.status_code == 200
    assert b"Back to model" in response.data
    assert (
        f'href="/projects/{project()["projectID"]}/models/{model()["modelDeploymentID"]}"'.encode()
        in response.data
    )
    assert b"ready" in response.data


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
    assert b"data-inference-stream-form" in response.data
    assert b"data-inference-output" in response.data
    assert b"data-inference-full-output" in response.data
    assert b"Full HTTP response" in response.data
