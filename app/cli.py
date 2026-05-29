"""Command-line client for the MiniTen HTTP API.

The CLI intentionally talks to the public HTTP API instead of importing service
functions. That keeps local/operator workflows on the same auth, validation,
idempotency, and response paths as dashboard users and external automation.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import requests


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
CONFIG_ENV = "MINITEN_CLI_CONFIG"
BASE_URL_ENV = "MINITEN_API_URL"
TOKEN_ENV = "MINITEN_ACCESS_TOKEN"
API_KEY_ENV = "MINITEN_PROJECT_API_KEY"
TOP_LEVEL_HELP = """command reference:
  config set-url <url>
  config show

  auth register --email <email> [--password <password>]
  auth login --email <email> [--password <password>]
  auth logout
  auth me
  auth delete-user

  projects create <name>
  projects list
  projects get <project-id>
  projects delete <project-id>

  members list <project-id>
  members add <project-id> --email <email> --role {owner,member,viewer}
  members update <project-id> <user-id> --role {owner,member,viewer}
  members remove <project-id> <user-id>

  api-keys create <project-id> <name> [--use]
  api-keys list <project-id>
  api-keys use <project-api-key>
  api-keys revoke <project-id> <api-key-id>

  models deploy <project-id> --name <name> --model-id <hf-model-id>
      [--replicas <n>] [--cpu-request <value>] [--cpu-limit <value>]
      [--memory-request <value>] [--memory-limit <value>] [--gpu-count <n>]
      [--dtype <dtype>] [--max-model-len <tokens>]
      [--autoscaling-enabled {true,false}] [--min-replicas <n>]
      [--max-replicas <n>] [--target-cpu-utilization <percent>]
      [--json <json-object>] [--idempotency-key <key>]
  models list <project-id>
  models get <project-id> <model-deployment-id>
  models update <project-id> <model-deployment-id> [model settings options]
      [--json <json-object>] [--idempotency-key <key>]
  models start <project-id> <model-deployment-id> [--idempotency-key <key>]
  models stop <project-id> <model-deployment-id> [--idempotency-key <key>]
  models sync <project-id> <model-deployment-id> [--idempotency-key <key>]
  models scale <project-id> <model-deployment-id> <replicas> [--idempotency-key <key>]
  models delete <project-id> <model-deployment-id> [--idempotency-key <key>]
  models jobs <project-id> <model-deployment-id>
  models status <project-id> <model-deployment-id>
  models logs <project-id> <model-name> [--tail <lines>]

  inference chat [--api-key <project-api-key>] [--model <name>]
      [--prompt <text>] [--max-tokens <n>] [--temperature <float>]
      [--stream] [--json <json-object>]
  inference models [--api-key <project-api-key>]

  analytics overview <project-id>
  analytics metrics <project-id> <model-name> [--since <iso8601>]
  analytics requests <project-id> <model-name> [--since <iso8601>]
      [--limit <n>] [--status-code <code>]
  analytics events <project-id> <model-name>

Run `miniten <group> <command> -h` for detailed help on one command.
"""


class CliError(RuntimeError):
    """Raised when the CLI cannot complete a user request."""


class CliState:
    """Persisted local CLI state such as base URL and bearer token."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_config_path()
        self.data = self.load()

    def load(self) -> dict[str, Any]:
        """Read config JSON from disk, returning an empty config if absent."""
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CliError(f"Invalid MiniTen CLI config: {self.path}") from exc

    def save(self) -> None:
        """Write config JSON with restrictive parent directory creation."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def base_url(self) -> str:
        """Return the active API base URL."""
        return os.getenv(BASE_URL_ENV) or self.data.get("base_url") or DEFAULT_BASE_URL

    def token(self) -> str | None:
        """Return the active user access token."""
        return os.getenv(TOKEN_ENV) or self.data.get("access_token")

    def project_api_key(self) -> str | None:
        """Return the active project API key for inference commands."""
        return os.getenv(API_KEY_ENV) or self.data.get("project_api_key")


class ApiClient:
    """Small HTTP API wrapper used by command handlers."""

    def __init__(self, state: CliState):
        self.state = state
        self.session = requests.Session()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        auth: bool = True,
        project_api_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Call the MiniTen API and return a parsed JSON response."""
        headers = {"Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if project_api_key:
            headers["Authorization"] = f"Bearer {project_api_key}"
        elif auth:
            token = self.state.token()
            if not token:
                raise CliError("Not logged in. Run `miniten auth login` first.")
            headers["Authorization"] = f"Bearer {token}"

        url = self.state.base_url().rstrip("/") + path
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=clean_none(query or {}),
                timeout=300,
            )
        except requests.RequestException as exc:
            raise CliError(f"Could not reach MiniTen API at {url}: {exc}") from exc

        body = parse_response_body(response)
        if response.status_code >= 400:
            message = error_message(body)
            raise CliError(f"{method} {path} failed with {response.status_code}: {message}")
        return body

    def stream_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any],
        project_api_key: str,
    ):
        """Call a streaming endpoint and yield decoded response chunks."""
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {project_api_key}",
        }
        url = self.state.base_url().rstrip("/") + path
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                stream=True,
                timeout=300,
            )
        except requests.RequestException as exc:
            raise CliError(f"Could not reach MiniTen API at {url}: {exc}") from exc

        try:
            if response.status_code >= 400:
                body = parse_response_body(response)
                message = error_message(body)
                raise CliError(
                    f"{method} {path} failed with {response.status_code}: {message}"
                )
            for chunk in response.iter_content(chunk_size=None, decode_unicode=True):
                if chunk:
                    yield chunk
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()


def default_config_path() -> Path:
    """Return the CLI config path."""
    override = os.getenv(CONFIG_ENV)
    if override:
        return Path(override)
    return Path.home() / ".miniten" / "config.json"


def parse_response_body(response: requests.Response) -> Any:
    """Parse a JSON response, falling back to text for non-JSON errors."""
    if not response.text:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"message": response.text}


def error_message(body: Any) -> str:
    """Extract a readable API error from a response body."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(body.get("message") or body)
    return str(body)


def clean_none(values: dict[str, Any]) -> dict[str, Any]:
    """Remove unset values before sending query strings."""
    return {key: value for key, value in values.items() if value is not None}


def print_json(value: Any) -> None:
    """Write command output as stable JSON."""
    print(json.dumps(value, indent=2, sort_keys=True))


def prompt_password(args: argparse.Namespace) -> str:
    """Return password from args or prompt without echo."""
    if args.password:
        return args.password
    return getpass.getpass("Password: ")


def idempotency_key(args: argparse.Namespace, action: str) -> str:
    """Return caller-provided idempotency key or generate a unique one."""
    return args.idempotency_key or f"cli-{action}-{uuid.uuid4()}"


def add_json_arg(parser: argparse.ArgumentParser) -> None:
    """Add a raw JSON request body option."""
    parser.add_argument("--json", dest="json_text", help="Raw JSON object body.")


def parse_json_arg(args: argparse.Namespace) -> dict[str, Any]:
    """Parse --json as an object."""
    if not args.json_text:
        return {}
    try:
        value = json.loads(args.json_text)
    except json.JSONDecodeError as exc:
        raise CliError(f"Invalid JSON body: {exc}") from exc
    if not isinstance(value, dict):
        raise CliError("--json must be a JSON object.")
    return value


def add_idempotency_arg(parser: argparse.ArgumentParser) -> None:
    """Add idempotency key option for control-plane commands."""
    parser.add_argument("--idempotency-key", help="Idempotency key for this command.")


def add_model_settings_args(parser: argparse.ArgumentParser) -> None:
    """Add common deployment settings flags."""
    parser.add_argument("--replicas", type=int)
    parser.add_argument("--cpu-request")
    parser.add_argument("--cpu-limit")
    parser.add_argument("--memory-request")
    parser.add_argument("--memory-limit")
    parser.add_argument("--gpu-count", type=int)
    parser.add_argument("--dtype")
    parser.add_argument("--max-model-len", type=int)
    parser.add_argument("--autoscaling-enabled", choices=["true", "false"])
    parser.add_argument("--min-replicas", type=int)
    parser.add_argument("--max-replicas", type=int)
    parser.add_argument("--target-cpu-utilization", type=int)


def model_settings_body(args: argparse.Namespace) -> dict[str, Any]:
    """Build a model deployment request body from CLI flags and --json."""
    body = parse_json_arg(args)
    if getattr(args, "name", None):
        body["name"] = args.name
    if getattr(args, "model_id", None):
        body["model_id"] = args.model_id
    if args.replicas is not None:
        body["replicas"] = args.replicas

    resources = body.setdefault("resources", {})
    if not isinstance(resources, dict):
        raise CliError("resources must be a JSON object.")
    for flag, field in [
        ("cpu_request", "cpu_request"),
        ("cpu_limit", "cpu_limit"),
        ("memory_request", "memory_request"),
        ("memory_limit", "memory_limit"),
        ("gpu_count", "gpu_count"),
    ]:
        value = getattr(args, flag)
        if value is not None:
            resources[field] = value
    if not resources:
        body.pop("resources", None)

    vllm = body.setdefault("vllm", {})
    if not isinstance(vllm, dict):
        raise CliError("vllm must be a JSON object.")
    if args.dtype is not None:
        vllm["dtype"] = args.dtype
    if args.max_model_len is not None:
        vllm["max_model_len"] = args.max_model_len
    if not vllm:
        body.pop("vllm", None)

    autoscaling = body.setdefault("autoscaling", {})
    if not isinstance(autoscaling, dict):
        raise CliError("autoscaling must be a JSON object.")
    if args.autoscaling_enabled is not None:
        autoscaling["enabled"] = args.autoscaling_enabled == "true"
    if args.min_replicas is not None:
        autoscaling["min_replicas"] = args.min_replicas
    if args.max_replicas is not None:
        autoscaling["max_replicas"] = args.max_replicas
    if args.target_cpu_utilization is not None:
        autoscaling["target_cpu_utilization"] = args.target_cpu_utilization
    if not autoscaling:
        body.pop("autoscaling", None)
    return body


def set_url(args: argparse.Namespace, state: CliState, _client: ApiClient) -> None:
    """Persist the API base URL."""
    state.data["base_url"] = args.url.rstrip("/")
    state.save()
    print_json({"base_url": state.data["base_url"]})


def show_config(_args: argparse.Namespace, state: CliState, _client: ApiClient) -> None:
    """Show current CLI configuration without printing bearer token contents."""
    print_json(
        {
            "base_url": state.base_url(),
            "config_path": str(state.path),
            "logged_in": bool(state.token()),
            "project_api_key_configured": bool(state.project_api_key()),
        }
    )


def auth_register(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Create a MiniTen user."""
    print_json(
        client.request(
            "POST",
            "/v1/users",
            json_body={"email": args.email, "password": prompt_password(args)},
            auth=False,
        )
    )


def auth_login(args: argparse.Namespace, state: CliState, client: ApiClient) -> None:
    """Login and persist access token."""
    body = client.request(
        "POST",
        "/v1/auth/login",
        json_body={"email": args.email, "password": prompt_password(args)},
        auth=False,
    )
    state.data["access_token"] = body["access_token"]
    state.save()
    print_json(body)


def auth_logout(_args: argparse.Namespace, state: CliState, client: ApiClient) -> None:
    """Logout from API and remove local token."""
    body = client.request("POST", "/v1/auth/logout")
    state.data.pop("access_token", None)
    state.save()
    print_json(body)


def user_me(_args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Show the current user."""
    print_json(client.request("GET", "/v1/users/me"))


def user_delete(_args: argparse.Namespace, state: CliState, client: ApiClient) -> None:
    """Delete the current user."""
    body = client.request("DELETE", "/v1/users/me")
    state.data.pop("access_token", None)
    state.save()
    print_json(body)


def project_create(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Create a project."""
    print_json(client.request("POST", "/v1/projects", json_body={"name": args.name}))


def project_list(_args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """List projects."""
    print_json(client.request("GET", "/v1/projects"))


def project_get(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Get a project."""
    print_json(client.request("GET", f"/v1/projects/{args.project_id}"))


def project_delete(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Delete a project."""
    print_json(client.request("DELETE", f"/v1/projects/{args.project_id}"))


def member_list(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """List project members."""
    print_json(client.request("GET", f"/v1/projects/{args.project_id}/members"))


def member_add(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Add a project member."""
    print_json(
        client.request(
            "POST",
            f"/v1/projects/{args.project_id}/members",
            json_body={"email": args.email, "role": args.role},
        )
    )


def member_update(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Update a project member role."""
    print_json(
        client.request(
            "PATCH",
            f"/v1/projects/{args.project_id}/members/{args.user_id}",
            json_body={"role": args.role},
        )
    )


def member_remove(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Remove a project member."""
    print_json(
        client.request(
            "DELETE",
            f"/v1/projects/{args.project_id}/members/{args.user_id}",
        )
    )


def api_key_create(args: argparse.Namespace, state: CliState, client: ApiClient) -> None:
    """Create a project API key."""
    body = client.request(
        "POST",
        f"/v1/projects/{args.project_id}/api-keys",
        json_body={"name": args.name},
    )
    if args.use:
        state.data["project_api_key"] = body["api_key"]
        state.save()
    print_json(body)


def api_key_list(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """List project API keys."""
    print_json(client.request("GET", f"/v1/projects/{args.project_id}/api-keys"))


def api_key_use(args: argparse.Namespace, state: CliState, _client: ApiClient) -> None:
    """Persist a project API key for inference commands."""
    state.data["project_api_key"] = args.api_key
    state.save()
    print_json({"project_api_key_configured": True})


def api_key_revoke(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Revoke a project API key."""
    print_json(
        client.request(
            "DELETE",
            f"/v1/projects/{args.project_id}/api-keys/{args.api_key_id}",
        )
    )


def model_deploy(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Create a model deployment."""
    print_json(
        client.request(
            "POST",
            f"/v1/projects/{args.project_id}/models",
            json_body=model_settings_body(args),
            idempotency_key=idempotency_key(args, "deploy"),
        )
    )


def model_list(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """List model deployments."""
    print_json(client.request("GET", f"/v1/projects/{args.project_id}/models"))


def model_get(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Get a model deployment."""
    print_json(
        client.request("GET", f"/v1/projects/{args.project_id}/models/{args.model_id}")
    )


def model_update(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Update model deployment settings."""
    print_json(
        client.request(
            "PATCH",
            f"/v1/projects/{args.project_id}/models/{args.model_id}",
            json_body=model_settings_body(args),
            idempotency_key=idempotency_key(args, "update"),
        )
    )


def model_command(
    args: argparse.Namespace,
    _state: CliState,
    client: ApiClient,
    command: str,
) -> None:
    """Run a model lifecycle command."""
    body = {"replicas": args.replicas} if command == "scale" else None
    print_json(
        client.request(
            "POST",
            f"/v1/projects/{args.project_id}/models/{args.model_id}/{command}",
            json_body=body,
            idempotency_key=idempotency_key(args, command),
        )
    )


def model_delete(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Delete a model deployment."""
    print_json(
        client.request(
            "DELETE",
            f"/v1/projects/{args.project_id}/models/{args.model_id}",
            idempotency_key=idempotency_key(args, "delete"),
        )
    )


def model_jobs(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """List model deployment jobs."""
    print_json(
        client.request(
            "GET",
            f"/v1/projects/{args.project_id}/models/{args.model_id}/jobs",
        )
    )


def model_status(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Show model deployment status."""
    print_json(
        client.request(
            "GET",
            f"/v1/projects/{args.project_id}/models/{args.model_id}/status",
        )
    )


def model_logs(args: argparse.Namespace, _state: CliState, client: ApiClient) -> None:
    """Read model logs by project-local model name."""
    print_json(
        client.request(
            "GET",
            f"/v1/projects/{args.project_id}/models/{args.model_name}/logs",
            query={"tail": args.tail},
        )
    )


def inference_chat(args: argparse.Namespace, state: CliState, client: ApiClient) -> None:
    """Call OpenAI-compatible chat completions using a project API key."""
    api_key = args.api_key or state.project_api_key()
    if not api_key:
        raise CliError(
            "No project API key configured. Pass --api-key or run "
            "`miniten api-keys use <key>`."
        )
    if not args.json_text and (not args.model or not args.prompt):
        raise CliError("Pass --model and --prompt, or provide a complete --json body.")
    body = parse_json_arg(args) or {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
    }
    if args.stream:
        body["stream"] = True
        print_chat_stream(
            client.stream_request(
                "POST",
                "/v1/chat/completions",
                json_body=body,
                project_api_key=api_key,
            )
        )
        return

    print_json(
        client.request(
            "POST",
            "/v1/chat/completions",
            json_body=body,
            auth=False,
            project_api_key=api_key,
        )
    )


def print_chat_stream(chunks) -> None:
    """Print OpenAI SSE chat deltas as they arrive."""
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            text = chat_stream_line_text(line)
            if text:
                print(text, end="", flush=True)
    if buffer:
        text = chat_stream_line_text(buffer)
        if text:
            print(text, end="", flush=True)
    print()


def chat_stream_line_text(line: str) -> str | None:
    """Extract printable text from one OpenAI SSE line."""
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return None

    payload_text = stripped.removeprefix("data:").strip()
    if not payload_text or payload_text == "[DONE]":
        return None

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return payload_text

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        return None

    first = choices[0]
    delta = first.get("delta") or {}
    return delta.get("content") or first.get("text")


def inference_models(args: argparse.Namespace, state: CliState, client: ApiClient) -> None:
    """List project API-key-visible models."""
    api_key = args.api_key or state.project_api_key()
    if not api_key:
        raise CliError(
            "No project API key configured. Pass --api-key or run "
            "`miniten api-keys use <key>`."
        )
    print_json(
        client.request(
            "GET",
            "/v1/models",
            auth=False,
            project_api_key=api_key,
        )
    )


def analytics_overview(
    args: argparse.Namespace,
    _state: CliState,
    client: ApiClient,
) -> None:
    """Show project analytics overview."""
    print_json(client.request("GET", f"/v1/projects/{args.project_id}/analytics/overview"))


def analytics_model_metrics(
    args: argparse.Namespace,
    _state: CliState,
    client: ApiClient,
) -> None:
    """Show model aggregate metrics."""
    print_json(
        client.request(
            "GET",
            f"/v1/projects/{args.project_id}/analytics/models/{args.model_name}/metrics",
            query={"since": args.since},
        )
    )


def analytics_model_requests(
    args: argparse.Namespace,
    _state: CliState,
    client: ApiClient,
) -> None:
    """Show model request metadata."""
    print_json(
        client.request(
            "GET",
            f"/v1/projects/{args.project_id}/analytics/models/{args.model_name}/requests",
            query={
                "since": args.since,
                "limit": args.limit,
                "status_code": args.status_code,
            },
        )
    )


def analytics_model_events(
    args: argparse.Namespace,
    _state: CliState,
    client: ApiClient,
) -> None:
    """Show model lifecycle events."""
    print_json(
        client.request(
            "GET",
            f"/v1/projects/{args.project_id}/analytics/models/{args.model_name}/events",
        )
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser tree."""
    parser = argparse.ArgumentParser(
        prog="miniten",
        description="MiniTen command-line client for the dashboard/API.",
        epilog=TOP_LEVEL_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(handler=lambda args, state, client: parser.print_help())
    subcommands = parser.add_subparsers(dest="resource")

    config = subcommands.add_parser("config")
    config_sub = config.add_subparsers(dest="command", required=True)
    config_set = config_sub.add_parser("set-url")
    config_set.add_argument("url")
    config_set.set_defaults(handler=set_url)
    config_show = config_sub.add_parser("show")
    config_show.set_defaults(handler=show_config)

    auth = subcommands.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="command", required=True)
    register = auth_sub.add_parser("register")
    register.add_argument("--email", required=True)
    register.add_argument("--password")
    register.set_defaults(handler=auth_register)
    login = auth_sub.add_parser("login")
    login.add_argument("--email", required=True)
    login.add_argument("--password")
    login.set_defaults(handler=auth_login)
    auth_sub.add_parser("logout").set_defaults(handler=auth_logout)
    auth_sub.add_parser("me").set_defaults(handler=user_me)
    auth_sub.add_parser("delete-user").set_defaults(handler=user_delete)

    projects = subcommands.add_parser("projects")
    projects_sub = projects.add_subparsers(dest="command", required=True)
    create_project = projects_sub.add_parser("create")
    create_project.add_argument("name")
    create_project.set_defaults(handler=project_create)
    projects_sub.add_parser("list").set_defaults(handler=project_list)
    get_project = projects_sub.add_parser("get")
    get_project.add_argument("project_id")
    get_project.set_defaults(handler=project_get)
    delete_project = projects_sub.add_parser("delete")
    delete_project.add_argument("project_id")
    delete_project.set_defaults(handler=project_delete)

    members = subcommands.add_parser("members")
    members_sub = members.add_subparsers(dest="command", required=True)
    list_members = members_sub.add_parser("list")
    list_members.add_argument("project_id")
    list_members.set_defaults(handler=member_list)
    add_member = members_sub.add_parser("add")
    add_member.add_argument("project_id")
    add_member.add_argument("--email", required=True)
    add_member.add_argument("--role", required=True, choices=["owner", "member", "viewer"])
    add_member.set_defaults(handler=member_add)
    update_member = members_sub.add_parser("update")
    update_member.add_argument("project_id")
    update_member.add_argument("user_id")
    update_member.add_argument("--role", required=True, choices=["owner", "member", "viewer"])
    update_member.set_defaults(handler=member_update)
    remove_member = members_sub.add_parser("remove")
    remove_member.add_argument("project_id")
    remove_member.add_argument("user_id")
    remove_member.set_defaults(handler=member_remove)

    api_keys = subcommands.add_parser("api-keys")
    api_keys_sub = api_keys.add_subparsers(dest="command", required=True)
    create_key = api_keys_sub.add_parser("create")
    create_key.add_argument("project_id")
    create_key.add_argument("name")
    create_key.add_argument("--use", action="store_true", help="Save returned key.")
    create_key.set_defaults(handler=api_key_create)
    list_keys = api_keys_sub.add_parser("list")
    list_keys.add_argument("project_id")
    list_keys.set_defaults(handler=api_key_list)
    use_key = api_keys_sub.add_parser("use")
    use_key.add_argument("api_key")
    use_key.set_defaults(handler=api_key_use)
    revoke_key = api_keys_sub.add_parser("revoke")
    revoke_key.add_argument("project_id")
    revoke_key.add_argument("api_key_id")
    revoke_key.set_defaults(handler=api_key_revoke)

    models = subcommands.add_parser("models")
    models_sub = models.add_subparsers(dest="command", required=True)
    deploy = models_sub.add_parser("deploy")
    deploy.add_argument("project_id")
    deploy.add_argument("--name", required=True)
    deploy.add_argument("--model-id", required=True)
    add_model_settings_args(deploy)
    add_json_arg(deploy)
    add_idempotency_arg(deploy)
    deploy.set_defaults(handler=model_deploy)
    list_models = models_sub.add_parser("list")
    list_models.add_argument("project_id")
    list_models.set_defaults(handler=model_list)
    get_model = models_sub.add_parser("get")
    get_model.add_argument("project_id")
    get_model.add_argument("model_id")
    get_model.set_defaults(handler=model_get)
    update_model = models_sub.add_parser("update")
    update_model.add_argument("project_id")
    update_model.add_argument("model_id")
    add_model_settings_args(update_model)
    add_json_arg(update_model)
    add_idempotency_arg(update_model)
    update_model.set_defaults(handler=model_update)
    for command in ["start", "stop", "hard-restart", "sync"]:
        command_parser = models_sub.add_parser(command)
        command_parser.add_argument("project_id")
        command_parser.add_argument("model_id")
        add_idempotency_arg(command_parser)
        command_parser.set_defaults(
            handler=lambda args, state, client, cmd=command: model_command(
                args,
                state,
                client,
                cmd,
            )
        )
    scale = models_sub.add_parser("scale")
    scale.add_argument("project_id")
    scale.add_argument("model_id")
    scale.add_argument("replicas", type=int)
    add_idempotency_arg(scale)
    scale.set_defaults(
        handler=lambda args, state, client: model_command(args, state, client, "scale")
    )
    delete_model = models_sub.add_parser("delete")
    delete_model.add_argument("project_id")
    delete_model.add_argument("model_id")
    add_idempotency_arg(delete_model)
    delete_model.set_defaults(handler=model_delete)
    jobs = models_sub.add_parser("jobs")
    jobs.add_argument("project_id")
    jobs.add_argument("model_id")
    jobs.set_defaults(handler=model_jobs)
    status = models_sub.add_parser("status")
    status.add_argument("project_id")
    status.add_argument("model_id")
    status.set_defaults(handler=model_status)
    logs = models_sub.add_parser("logs")
    logs.add_argument("project_id")
    logs.add_argument("model_name")
    logs.add_argument("--tail", type=int)
    logs.set_defaults(handler=model_logs)

    inference = subcommands.add_parser("inference")
    inference_sub = inference.add_subparsers(dest="command", required=True)
    chat = inference_sub.add_parser("chat")
    chat.add_argument("--api-key")
    chat.add_argument("--model")
    chat.add_argument("--prompt")
    chat.add_argument("--max-tokens", type=int, default=128)
    chat.add_argument("--temperature", type=float, default=0)
    chat.add_argument("--stream", action="store_true")
    add_json_arg(chat)
    chat.set_defaults(handler=inference_chat)
    inf_models = inference_sub.add_parser("models")
    inf_models.add_argument("--api-key")
    inf_models.set_defaults(handler=inference_models)

    analytics = subcommands.add_parser("analytics")
    analytics_sub = analytics.add_subparsers(dest="command", required=True)
    overview = analytics_sub.add_parser("overview")
    overview.add_argument("project_id")
    overview.set_defaults(handler=analytics_overview)
    metrics = analytics_sub.add_parser("metrics")
    metrics.add_argument("project_id")
    metrics.add_argument("model_name")
    metrics.add_argument("--since")
    metrics.set_defaults(handler=analytics_model_metrics)
    requests_parser = analytics_sub.add_parser("requests")
    requests_parser.add_argument("project_id")
    requests_parser.add_argument("model_name")
    requests_parser.add_argument("--since")
    requests_parser.add_argument("--limit", type=int)
    requests_parser.add_argument("--status-code", type=int)
    requests_parser.set_defaults(handler=analytics_model_requests)
    events = analytics_sub.add_parser("events")
    events.add_argument("project_id")
    events.add_argument("model_name")
    events.set_defaults(handler=analytics_model_events)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    state = CliState()
    client = ApiClient(state)
    try:
        args.handler(args, state, client)
    except CliError as exc:
        print(f"miniten: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
