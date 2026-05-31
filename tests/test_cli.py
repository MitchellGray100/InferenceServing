from __future__ import annotations

import json

import pytest

from app import cli


class FakeResponse:
    def __init__(self, status_code: int, body: dict | list | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body) if body is not None else ""

    def json(self):
        return self._body

    def iter_content(self, chunk_size=None, decode_unicode=False):
        chunks = [self.text]
        yield from chunks

    def close(self):
        pass


@pytest.fixture
def cli_config(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setenv("MINITEN_CLI_CONFIG", str(path))
    monkeypatch.delenv("MINITEN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MINITEN_PROJECT_API_KEY", raising=False)
    monkeypatch.delenv("MINITEN_API_URL", raising=False)
    return path


@pytest.fixture
def calls(monkeypatch):
    requests = []

    def fake_request(self, method, url, **kwargs):
        requests.append({"method": method, "url": url, **kwargs})
        return FakeResponse(200, {"ok": True})

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)
    return requests


def write_config(path, **values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values), encoding="utf-8")


def test_login_persists_user_access_token(cli_config, monkeypatch, capsys):
    def fake_request(self, method, url, **kwargs):
        return FakeResponse(200, {"access_token": "user-token"})

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)

    exit_code = cli.main(
        ["auth", "login", "--email", "person@example.com", "--password", "secret"]
    )

    assert exit_code == 0
    assert json.loads(cli_config.read_text(encoding="utf-8"))["access_token"] == (
        "user-token"
    )
    assert json.loads(capsys.readouterr().out)["access_token"] == "user-token"


def test_authenticated_command_uses_saved_bearer_token(cli_config, calls):
    write_config(cli_config, access_token="user-token")

    assert cli.main(["projects", "list"]) == 0

    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://127.0.0.1:8000/v1/projects"
    assert calls[0]["headers"]["Authorization"] == "Bearer user-token"


def test_api_key_create_can_save_project_key(cli_config, monkeypatch):
    write_config(cli_config, access_token="user-token")

    def fake_request(self, method, url, **kwargs):
        return FakeResponse(201, {"api_key": "project-key", "id": "key-id"})

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)

    assert cli.main(["api-keys", "create", "project-id", "local", "--use"]) == 0

    assert json.loads(cli_config.read_text(encoding="utf-8"))["project_api_key"] == (
        "project-key"
    )


def test_model_deploy_sends_settings(cli_config, calls):
    write_config(cli_config, access_token="user-token")

    exit_code = cli.main(
        [
            "models",
            "deploy",
            "project-id",
            "--name",
            "qwen",
            "--model-id",
            "Qwen/Qwen2.5-0.5B-Instruct",
            "--cpu-request",
            "2",
            "--memory-limit",
            "8Gi",
            "--gpu-count",
            "1",
            "--dtype",
            "auto",
            "--max-model-len",
            "1024",
            "--autoscaling-enabled",
            "true",
            "--min-replicas",
            "1",
            "--max-replicas",
            "2",
        ]
    )

    assert exit_code == 0
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/v1/projects/project-id/models")
    assert calls[0]["json"] == {
        "name": "qwen",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "resources": {
            "cpu_request": "2",
            "memory_limit": "8Gi",
            "gpu_count": 1,
        },
        "vllm": {"dtype": "auto", "max_model_len": 1024},
        "autoscaling": {
            "enabled": True,
            "min_replicas": 1,
            "max_replicas": 2,
        },
    }


def test_inference_chat_uses_project_api_key(cli_config, calls):
    write_config(cli_config, project_api_key="project-key")

    exit_code = cli.main(
        [
            "inference",
            "chat",
            "--model",
            "qwen",
            "--prompt",
            "Say hello",
            "--max-tokens",
            "8",
        ]
    )

    assert exit_code == 0
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer project-key"
    assert calls[0]["json"]["messages"] == [{"role": "user", "content": "Say hello"}]


def test_inference_chat_stream_prints_deltas(cli_config, monkeypatch, capsys):
    write_config(cli_config, project_api_key="project-key")
    calls = []

    class StreamingResponse(FakeResponse):
        def __init__(self):
            super().__init__(200, None)
            self.text = ""

        def iter_content(self, chunk_size=None, decode_unicode=False):
            yield 'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
            yield 'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
            yield "data: [DONE]\n\n"

    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return StreamingResponse()

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)

    exit_code = cli.main(
        [
            "inference",
            "chat",
            "--model",
            "qwen",
            "--prompt",
            "Say hello",
            "--stream",
        ]
    )

    assert exit_code == 0
    assert calls[0]["stream"] is True
    assert calls[0]["headers"]["Accept"] == "text/event-stream"
    assert calls[0]["json"]["stream"] is True
    assert capsys.readouterr().out == "Hello\n"


def test_inference_requires_project_api_key(cli_config, capsys):
    exit_code = cli.main(
        ["inference", "chat", "--model", "qwen", "--prompt", "Say hello"]
    )

    assert exit_code == 1
    assert "No project API key configured" in capsys.readouterr().err


def test_http_error_is_printed(cli_config, monkeypatch, capsys):
    write_config(cli_config, access_token="user-token")

    def fake_request(self, method, url, **kwargs):
        return FakeResponse(403, {"error": {"message": "forbidden"}})

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)

    assert cli.main(["projects", "list"]) == 1
    assert "GET /v1/projects failed with 403: forbidden" in capsys.readouterr().err


def test_project_delete_requires_confirmation(cli_config, calls, monkeypatch, capsys):
    write_config(cli_config, access_token="user-token")
    monkeypatch.setattr("builtins.input", lambda prompt: "no")

    exit_code = cli.main(["projects", "delete", "project-id"])

    assert exit_code == 1
    assert calls == []
    assert "Cancelled." in capsys.readouterr().err


def test_project_delete_yes_skips_confirmation(cli_config, calls):
    write_config(cli_config, access_token="user-token")

    exit_code = cli.main(["projects", "delete", "project-id", "--yes"])

    assert exit_code == 0
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"].endswith("/v1/projects/project-id")


def test_user_delete_yes_clears_saved_token(cli_config, calls):
    write_config(cli_config, access_token="user-token")

    exit_code = cli.main(["auth", "delete-user", "--yes"])

    assert exit_code == 0
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"].endswith("/v1/users/me")
    assert "access_token" not in json.loads(cli_config.read_text(encoding="utf-8"))


def test_model_delete_requires_confirmation(cli_config, calls, monkeypatch, capsys):
    write_config(cli_config, access_token="user-token")
    monkeypatch.setattr("builtins.input", lambda prompt: "no")

    exit_code = cli.main(["models", "delete", "project-id", "model-id"])

    assert exit_code == 1
    assert calls == []
    assert "Cancelled." in capsys.readouterr().err


def test_model_hard_restart_yes_skips_confirmation(cli_config, calls):
    write_config(cli_config, access_token="user-token")

    exit_code = cli.main(
        ["models", "hard-restart", "project-id", "model-id", "--yes"]
    )

    assert exit_code == 0
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith(
        "/v1/projects/project-id/models/model-id/hard-restart"
    )


def test_model_retry_uses_start_api_path(cli_config, calls):
    write_config(cli_config, access_token="user-token")

    exit_code = cli.main(["models", "retry", "project-id", "model-id"])

    assert exit_code == 0
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/v1/projects/project-id/models/model-id/start")


def test_top_level_help_shows_command_inputs(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.build_parser().parse_args(["-h"])

    output = capsys.readouterr().out
    assert exit_info.value.code == 0
    assert "command reference:" in output
    assert "auth login --email <email> [--password <password>]" in output
    assert "auth delete-user [--yes]" in output
    assert "models deploy <project-id> --name <name> --model-id <hf-model-id>" in output
    assert "models retry <project-id> <model-deployment-id>" in output
    assert "inference chat [--api-key <project-api-key>]" in output
