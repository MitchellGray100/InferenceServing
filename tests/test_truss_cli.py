from __future__ import annotations

import json

import pytest

from app import cli, truss_cli
from tests.test_cli import FakeResponse


@pytest.fixture
def truss_config(monkeypatch, tmp_path):
    path = tmp_path / "miniten-config.json"
    monkeypatch.setenv("MINITEN_CLI_CONFIG", str(path))
    monkeypatch.delenv("MINITEN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MINITEN_API_URL", raising=False)
    monkeypatch.delenv("MINITEN_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return path


def test_truss_login_stores_account_api_key(truss_config, monkeypatch, capsys) -> None:
    prompts = []
    monkeypatch.setattr(
        truss_cli.getpass,
        "getpass",
        lambda prompt: prompts.append(prompt) or "account-key",
    )

    exit_code = truss_cli.main(["login", "--base-url", "http://localhost:9000"])

    assert exit_code == 0
    saved = json.loads(truss_config.read_text(encoding="utf-8"))
    assert saved["account_api_key"] == "account-key"
    assert "project_api_key" not in saved
    assert saved["base_url"] == "http://localhost:9000"
    assert prompts == ["🤫 Quietly paste your API_KEY: "]
    output = capsys.readouterr().out
    assert "💻 Let's add a MiniTen remote!" in output
    assert "Configured MiniTen account API key." in output


def test_truss_top_level_help_documents_commands(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        truss_cli.main(["-h"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "MiniTen Truss-style deployment wrapper." in output
    assert "command reference:" in output
    assert "login [--api-key <account-api-key>] [--base-url <url>]" in output
    assert "init <project-name> [--model-name <deployment-name>]" in output
    assert "push [--config <path>] [--poll-interval <seconds>] [--no-watch]" in output
    assert "watch [--config <path>] [--poll-interval <seconds>]" in output
    assert "config.yaml fields:" in output
    assert "truss push watches config.yaml by default" in output


def test_truss_subcommand_help_documents_options(capsys) -> None:
    expected = {
        "login": [
            "--api-key <account-api-key>",
            "--base-url <url>",
            "truss login --api-key mt_account_...",
        ],
        "init": [
            "<project-name>",
            "--model-name <deployment-name>",
            "truss init qwen-2.5-3b",
        ],
        "push": [
            "--config <path>",
            "--poll-interval <seconds>",
            "--no-watch",
            "truss push --poll-interval 2",
        ],
        "watch": [
            "--config <path>",
            "--poll-interval <seconds>",
            "truss watch --poll-interval 2",
        ],
    }

    for command, snippets in expected.items():
        with pytest.raises(SystemExit) as exc:
            truss_cli.main([command, "-h"])

        assert exc.value.code == 0
        output = capsys.readouterr().out
        for snippet in snippets:
            assert snippet in output


def test_truss_login_prompts_even_when_miniten_api_key_env_exists(
    truss_config,
    monkeypatch,
) -> None:
    prompts = []
    monkeypatch.setenv("MINITEN_API_KEY", "env-account-key")
    monkeypatch.setattr(
        truss_cli.getpass,
        "getpass",
        lambda prompt: prompts.append(prompt) or "prompt-account-key",
    )

    exit_code = truss_cli.main(["login"])

    assert exit_code == 0
    saved = json.loads(truss_config.read_text(encoding="utf-8"))
    assert saved["account_api_key"] == "prompt-account-key"
    assert prompts == ["🤫 Quietly paste your API_KEY: "]


def test_truss_login_api_key_arg_skips_prompt(truss_config, monkeypatch) -> None:
    prompts = []
    monkeypatch.setenv("MINITEN_API_KEY", "env-account-key")
    monkeypatch.setattr(
        truss_cli.getpass,
        "getpass",
        lambda prompt: prompts.append(prompt) or "prompt-account-key",
    )

    exit_code = truss_cli.main(["login", "--api-key", "arg-account-key"])

    assert exit_code == 0
    saved = json.loads(truss_config.read_text(encoding="utf-8"))
    assert saved["account_api_key"] == "arg-account-key"
    assert prompts == []


def test_truss_init_creates_project_directory_and_config(
    truss_config,
    monkeypatch,
    capsys,
) -> None:
    truss_config.write_text(
        json.dumps({"account_api_key": "account-key"}),
        encoding="utf-8",
    )
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(
            200,
            {"project": {"projectID": "project-id", "name": "qwen-2.5-3b"}},
        )

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)
    def fake_input(prompt):
        print(prompt, end="")
        return "qwen-2-5-3b"

    monkeypatch.setattr("builtins.input", fake_input)

    exit_code = truss_cli.main(["init", "qwen-2.5-3b"])

    assert exit_code == 0
    config_path = truss_config.parent / "qwen-2.5-3b" / "config.yaml"
    assert config_path.exists()
    config_text = config_path.read_text(encoding="utf-8")
    assert "model_name: qwen-2-5-3b" in config_text
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/v1/truss/projects/init")
    assert calls[0]["headers"]["Authorization"] == "Bearer account-key"
    assert calls[0]["json"] == {"name": "qwen-2.5-3b"}
    output = capsys.readouterr().out
    assert "📦 Name this model: " in output
    assert "Truss qwen-2-5-3b was created in" in output
    assert "/qwen-2.5-3b" in output.replace("\\", "/")


def test_truss_init_prompts_for_missing_account_key(
    truss_config,
    monkeypatch,
) -> None:
    prompts = []
    calls = []
    monkeypatch.setattr(
        truss_cli.getpass,
        "getpass",
        lambda prompt: prompts.append(prompt) or "prompt-account-key",
    )

    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(
            200,
            {"project": {"projectID": "project-id", "name": "qwen-2.5-3b"}},
        )

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)

    exit_code = truss_cli.main(["init", "qwen-2.5-3b", "--model-name", "qwen-2-5-3b"])

    assert exit_code == 0
    assert prompts == ["🤫 Quietly paste your API_KEY: "]
    assert calls[0]["headers"]["Authorization"] == "Bearer prompt-account-key"
    saved = json.loads(truss_config.read_text(encoding="utf-8"))
    assert saved["account_api_key"] == "prompt-account-key"


def test_truss_init_preserves_existing_config(truss_config, monkeypatch, capsys) -> None:
    truss_config.write_text(
        json.dumps({"account_api_key": "account-key"}),
        encoding="utf-8",
    )
    project_dir = truss_config.parent / "qwen-2.5-3b"
    project_dir.mkdir()
    config_path = project_dir / "config.yaml"
    config_path.write_text("model_name: existing\n", encoding="utf-8")

    def fake_request(self, method, url, **kwargs):
        return FakeResponse(
            200,
            {"project": {"projectID": "project-id", "name": "qwen-2.5-3b"}},
        )

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)

    exit_code = truss_cli.main(["init", "qwen-2.5-3b", "--model-name", "qwen-2-5-3b"])

    assert exit_code == 0
    assert config_path.read_text(encoding="utf-8") == "model_name: existing\n"
    assert "left unchanged" in capsys.readouterr().out


def test_truss_push_uses_current_directory_as_project_name(
    truss_config,
    monkeypatch,
    capsys,
) -> None:
    truss_config.write_text(
        json.dumps({"account_api_key": "account-key"}),
        encoding="utf-8",
    )
    project_dir = truss_config.parent / "qwen-2.5-3b"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    (project_dir / "config.yaml").write_text(
        """
model_name: qwen
model_id: Qwen/Qwen2.5-0.5B-Instruct
replicas: 1
resources:
  cpu_request: "2"
  memory_limit: "8Gi"
vllm:
  dtype: auto
  max_model_len: 1024
autoscaling:
  enabled: false
""",
        encoding="utf-8",
    )
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(
            201,
            {
                "modelDeployment": {
                    "modelDeploymentID": "abc1d2ef",
                    "projectID": "project-id",
                    "name": "qwen",
                },
                "deploymentJob": {"deploymentJobID": "xyz123"},
            },
        )

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)

    exit_code = truss_cli.main(["push", "--no-watch"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "✨ Model qwen was successfully pushed ✨" in output
    assert "🪵 View logs for your deployment at" in output
    assert "👀 Watching for changes to truss..." in output
    assert "http://127.0.0.1:8000/projects/project-id/models/qwen/logs" in output
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/v1/truss/models")
    assert calls[0]["headers"]["Authorization"] == "Bearer account-key"
    assert calls[0]["json"] == {
        "project_name": "qwen-2.5-3b",
        "deployment": {
            "name": "qwen",
            "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
            "replicas": 1,
            "resources": {
                "cpu_request": "2",
                "memory_limit": "8Gi",
            },
            "vllm": {
                "dtype": "auto",
                "max_model_len": 1024,
            },
            "autoscaling": {
                "enabled": False,
            },
        },
    }


def test_truss_push_watches_by_default(
    truss_config,
    monkeypatch,
) -> None:
    truss_config.write_text(
        json.dumps({"account_api_key": "account-key"}),
        encoding="utf-8",
    )
    project_dir = truss_config.parent / "qwen-2.5-3b"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    config_path = project_dir / "config.yaml"
    config_path.write_text(
        """
model_name: qwen
model_id: Qwen/Qwen2.5-0.5B-Instruct
""",
        encoding="utf-8",
    )

    def fake_request(self, method, url, **kwargs):
        return FakeResponse(
            201,
            {
                "modelDeployment": {
                    "modelDeploymentID": "abc1d2ef",
                    "projectID": "project-id",
                    "name": "qwen",
                },
                "deploymentJob": {"deploymentJobID": "xyz123"},
            },
        )

    watched = []

    def fake_watch_config(
        path,
        state,
        client,
        poll_interval,
        *,
        once=False,
        initial_status=True,
    ):
        watched.append(
            {
                "path": path,
                "state": state,
                "client": client,
                "poll_interval": poll_interval,
                "once": once,
                "initial_status": initial_status,
            }
        )

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)
    monkeypatch.setattr(truss_cli, "watch_config", fake_watch_config)

    exit_code = truss_cli.main(["push", "--poll-interval", "0.25"])

    assert exit_code == 0
    assert watched
    assert watched[0]["path"] == config_path.relative_to(project_dir)
    assert watched[0]["poll_interval"] == 0.25
    assert watched[0]["once"] is False
    assert watched[0]["initial_status"] is False


def test_truss_push_validates_model_name_before_api_call(
    truss_config,
    monkeypatch,
    capsys,
) -> None:
    truss_config.write_text(
        json.dumps({"account_api_key": "account-key"}),
        encoding="utf-8",
    )
    project_dir = truss_config.parent / "qwen-2.5-3b"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    (project_dir / "config.yaml").write_text(
        """
model_name: Qwen 2.5 3B
model_id: Qwen/Qwen2.5-0.5B-Instruct
""",
        encoding="utf-8",
    )
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(201, {})

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)

    exit_code = truss_cli.main(["push", "--no-watch"])

    assert exit_code == 1
    assert calls == []
    assert "config.yaml model_name is invalid" in capsys.readouterr().err


def test_truss_push_requires_config(truss_config, capsys) -> None:
    truss_config.write_text(
        json.dumps({"account_api_key": "account-key"}),
        encoding="utf-8",
    )

    exit_code = truss_cli.main(["push"])

    assert exit_code == 1
    assert "No config.yaml found" in capsys.readouterr().err


def test_truss_watch_once_reports_no_changes(
    truss_config,
    monkeypatch,
    capsys,
) -> None:
    truss_config.write_text(
        json.dumps({"account_api_key": "account-key"}),
        encoding="utf-8",
    )
    project_dir = truss_config.parent / "qwen-2.5-3b"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    (project_dir / "config.yaml").write_text(
        "model_name: qwen\nmodel_id: Qwen/Qwen2.5-0.5B-Instruct\n",
        encoding="utf-8",
    )

    exit_code = truss_cli.main(["watch", "--once"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "🚰 Attempting to sync truss with remote" in output
    assert "No changes observed, skipping patching." in output
    assert "👀 Watching for changes to truss..." in output


def test_truss_watch_once_patches_when_config_changed_since_push(
    truss_config,
    monkeypatch,
    capsys,
) -> None:
    truss_config.write_text(
        json.dumps({"account_api_key": "account-key"}),
        encoding="utf-8",
    )
    project_dir = truss_config.parent / "qwen-2.5-3b"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    config_path = project_dir / "config.yaml"
    config_path.write_text(
        "model_name: qwen\nmodel_id: Qwen/Qwen2.5-0.5B-Instruct\n",
        encoding="utf-8",
    )
    state = cli.CliState()
    truss_cli.save_config_digest(state, config_path, truss_cli.file_digest(config_path))
    config_path.write_text(
        "model_name: qwen\n"
        "model_id: Qwen/Qwen2.5-0.5B-Instruct\n"
        "vllm:\n"
        "  max_model_len: 2048\n",
        encoding="utf-8",
    )
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(
            202,
            {
                "modelDeployment": {
                    "modelDeploymentID": "abc1d2ef",
                    "projectID": "project-id",
                    "name": "qwen",
                },
                "deploymentJob": {"deploymentJobID": "xyz123"},
            },
        )

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)

    exit_code = truss_cli.main(["watch", "--once"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Changes observed, patching." in output
    assert "No changes observed, skipping patching." not in output
    assert calls[0]["method"] == "PATCH"
    saved = json.loads(truss_config.read_text(encoding="utf-8"))
    assert saved["truss_config_digests"][str(config_path.resolve())] == (
        truss_cli.file_digest(config_path)
    )


def test_sync_truss_update_queues_patch_for_changed_config(
    truss_config,
    monkeypatch,
) -> None:
    truss_config.write_text(
        json.dumps({"account_api_key": "account-key"}),
        encoding="utf-8",
    )
    project_dir = truss_config.parent / "qwen-2.5-3b"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    config_path = project_dir / "config.yaml"
    config_path.write_text(
        """
model_name: qwen
model_id: Qwen/Qwen2.5-0.5B-Instruct
vllm:
  max_model_len: 2048
""",
        encoding="utf-8",
    )
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(
            202,
            {
                "modelDeployment": {
                    "modelDeploymentID": "abc1d2ef",
                    "projectID": "project-id",
                    "name": "qwen",
                },
                "deploymentJob": {"deploymentJobID": "xyz123"},
            },
        )

    monkeypatch.setattr(cli.requests.Session, "request", fake_request)

    state = cli.CliState()
    client = cli.ApiClient(state)
    response = truss_cli.sync_truss_update(config_path, state, client)

    assert response["modelDeployment"]["name"] == "qwen"
    assert calls[0]["method"] == "PATCH"
    assert calls[0]["url"].endswith("/v1/truss/models")
    assert calls[0]["headers"]["Authorization"] == "Bearer account-key"
    assert calls[0]["json"] == {
        "project_name": "qwen-2.5-3b",
        "deployment": {
            "name": "qwen",
            "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
            "vllm": {"max_model_len": 2048},
        },
    }


def test_build_dashboard_logs_url_strips_api_prefix() -> None:
    assert truss_cli.build_dashboard_logs_url(
        "http://localhost:8000/v1",
        "project id",
        "qwen/small",
    ) == "http://localhost:8000/projects/project%20id/models/qwen%2Fsmall/logs"
