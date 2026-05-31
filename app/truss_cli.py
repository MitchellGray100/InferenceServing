"""Small Truss-style wrapper over the MiniTen CLI/API."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import sys
import time
from pathlib import Path
from textwrap import dedent
from typing import Any
from urllib.parse import quote

import yaml

from app import cli
from app.utils.errors import ValidationError
from app.utils.validation import validate_deployment_name


DEFAULT_CONFIG_FILE = "config.yaml"
API_KEY_ENV = "MINITEN_API_KEY"
STARTER_CONFIG_TEMPLATE = """# MiniTen model deployment spec.
model_name: {model_name}
model_id: HuggingFaceTB/SmolLM2-135M-Instruct
replicas: 1

resources:
  cpu_request: "2"
  cpu_limit: "4"
  memory_request: "4Gi"
  memory_limit: "12Gi"
  gpu_count: 0

vllm:
  dtype: auto
  max_model_len: 512

autoscaling:
  enabled: false
"""


def login(args: argparse.Namespace, state: cli.CliState, _client: cli.ApiClient) -> None:
    """Store a MiniTen account API key for future Truss commands."""
    token = account_api_key_from_arg_or_prompt(args.api_key)
    if args.base_url:
        state.data["base_url"] = args.base_url.rstrip("/")
    store_account_api_key(state, token)
    state.save()
    print("Configured MiniTen account API key.")


def init(args: argparse.Namespace, state: cli.CliState, client: cli.ApiClient) -> None:
    """Create-or-return a MiniTen project and write a local config.yaml."""
    account_api_key = require_account_api_key(state)
    model_name = model_name_from_arg_or_prompt(args.model_name)
    response = client.request(
        "POST",
        "/v1/truss/projects/init",
        json_body={"name": args.name},
        auth=False,
        project_api_key=account_api_key,
    )
    project_dir = Path(args.name)
    config_path = project_dir / DEFAULT_CONFIG_FILE
    created = write_starter_config(config_path, model_name=model_name)
    project = response.get("project", {}) if isinstance(response, dict) else {}
    project_name = str(project.get("name", args.name))
    if created:
        print(f"Truss {model_name} was created in {display_path(project_dir)}")
    else:
        print(f"Project {project_name} is ready.")
        print(f"Existing {config_path} left unchanged.")


def push(args: argparse.Namespace, state: cli.CliState, client: cli.ApiClient) -> None:
    """Read ./config.yaml and deploy into the project named by this directory."""
    config_path = resolve_config_path(args.config)
    spec = load_config(config_path)
    account_api_key = require_account_api_key(state)
    body = deployment_body_from_spec(spec)
    validate_config_model_name(body["name"])
    project_name = Path.cwd().name
    response = client.request(
        "POST",
        "/v1/truss/models",
        json_body={"project_name": project_name, "deployment": body},
        auth=False,
        project_api_key=account_api_key,
    )
    save_config_digest(state, config_path, file_digest(config_path))
    print_push_success(body["name"], client.state.base_url(), response)
    if not args.no_watch:
        watch_config(config_path, state, client, args.poll_interval, initial_status=False)


def watch(args: argparse.Namespace, state: cli.CliState, client: cli.ApiClient) -> None:
    """Watch config.yaml and queue update jobs when it changes."""
    config_path = resolve_config_path(args.config)
    watch_config(config_path, state, client, args.poll_interval, once=args.once)


def write_starter_config(path: Path, *, model_name: str) -> bool:
    """Create a starter config file if one does not already exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return False
    path.write_text(
        STARTER_CONFIG_TEMPLATE.format(
            model_name=model_name,
        ),
        encoding="utf-8",
    )
    return True


def resolve_config_path(config: str | None) -> Path:
    """Find the YAML config for `truss push`."""
    if config:
        return Path(config)
    path = Path(DEFAULT_CONFIG_FILE)
    if path.exists():
        return path
    raise cli.CliError("No config.yaml found. Run `truss init <project-name>` first.")


def watch_config(
    config_path: Path,
    state: cli.CliState,
    client: cli.ApiClient,
    poll_interval: float,
    *,
    once: bool = False,
    initial_status: bool = True,
) -> None:
    """Watch a config file and sync updates when the file content changes."""
    current_digest = file_digest(config_path)
    pushed_digest = load_config_digest(state, config_path)
    if pushed_digest and pushed_digest != current_digest:
        print("🚰 Attempting to sync truss with remote")
        print("Changes observed, patching.")
        response = sync_truss_update(config_path, state, client)
        print_update_success(config_path, client, response)
        last_digest = current_digest
    else:
        last_digest = current_digest
        if initial_status:
            print("🚰 Attempting to sync truss with remote")
            print("No changes observed, skipping patching.")

    if initial_status:
        print("👀 Watching for changes to truss...")
    if once:
        return

    try:
        while True:
            time.sleep(poll_interval)
            current_digest = file_digest(config_path)
            if current_digest == last_digest:
                continue
            last_digest = current_digest
            print("🚰 Attempting to sync truss with remote")
            print("Changes observed, patching.")
            response = sync_truss_update(config_path, state, client)
            print_update_success(config_path, client, response)
            print("👀 Watching for changes to truss...")
    except KeyboardInterrupt:
        print("Stopped watching truss.")


def sync_truss_update(
    config_path: Path,
    state: cli.CliState,
    client: cli.ApiClient,
) -> dict[str, Any]:
    """Queue a model update for the current config.yaml."""
    spec = load_config(config_path)
    account_api_key = require_account_api_key(state)
    body = deployment_body_from_spec(spec)
    validate_config_model_name(body["name"])
    project_name = Path.cwd().name
    response = client.request(
        "PATCH",
        "/v1/truss/models",
        json_body={"project_name": project_name, "deployment": body},
        auth=False,
        project_api_key=account_api_key,
    )
    save_config_digest(state, config_path, file_digest(config_path))
    return response if isinstance(response, dict) else {}


def file_digest(path: Path) -> str:
    """Return a stable digest for a watched file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_digest_key(path: Path) -> str:
    """Build a stable key for one watched config file."""
    return str(path.resolve())


def load_config_digest(state: cli.CliState, path: Path) -> str | None:
    """Return the last successfully pushed digest for one config file."""
    digests = state.data.get("truss_config_digests")
    if not isinstance(digests, dict):
        return None
    value = digests.get(config_digest_key(path))
    return str(value) if value else None


def save_config_digest(state: cli.CliState, path: Path, digest: str) -> None:
    """Persist the last successfully pushed digest for one config file."""
    digests = state.data.get("truss_config_digests")
    if not isinstance(digests, dict):
        digests = {}
    digests[config_digest_key(path)] = digest
    state.data["truss_config_digests"] = digests
    state.save()


def account_api_key_from_arg_or_prompt(value: str | None) -> str:
    """Return an account API key from an argument or explicit login prompt."""
    token = value
    if token is None:
        print("💻 Let's add a MiniTen remote!")
        token = getpass.getpass("🤫 Quietly paste your API_KEY: ")
    token = token.strip()
    if not token:
        raise cli.CliError("Account API key is required.")
    os.environ[API_KEY_ENV] = token
    return token


def model_name_from_arg_or_prompt(value: str | None) -> str:
    """Return a MiniTen deployment model name from an argument or init prompt."""
    name = value
    if name is None:
        name = input("📦 Name this model: ")
    name = name.strip()
    if not name:
        raise cli.CliError("Model name is required.")
    return name


def display_path(path: Path) -> str:
    """Return a compact user-facing path for init output."""
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(Path.home())
    except ValueError:
        return str(path)
    return f"~/{relative.as_posix()}"


def require_account_api_key(state: cli.CliState) -> str:
    """Return a configured account API key, prompting if absent."""
    token = os.environ.get(API_KEY_ENV) or state.data.get("account_api_key")
    if token:
        os.environ[API_KEY_ENV] = str(token)
        return str(token)

    token = account_api_key_from_arg_or_prompt(None)
    state.data["account_api_key"] = token
    state.save()
    return token


def store_account_api_key(state: cli.CliState, token: str) -> None:
    """Persist the account API key for later Truss invocations."""
    state.data["account_api_key"] = token
    state.data.pop("project_api_key", None)


def load_config(path: Path) -> dict[str, Any]:
    """Load and validate a YAML object."""
    if not path.exists():
        raise cli.CliError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise cli.CliError(f"Invalid YAML config: {exc}") from exc
    if not isinstance(data, dict):
        raise cli.CliError("config.yaml must contain a YAML object.")
    return data


def deployment_body_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Convert the YAML spec into the existing MiniTen model deploy body."""
    name = string_field(spec, "name") or string_field(spec, "model_name")
    model_id = string_field(spec, "model_id")
    if not name:
        raise cli.CliError("config.yaml must set model_name or name.")
    if not model_id:
        raise cli.CliError("config.yaml must set model_id.")

    body: dict[str, Any] = {
        "name": name,
        "model_id": model_id,
    }
    if spec.get("replicas") is not None:
        body["replicas"] = spec["replicas"]

    for section in ["resources", "vllm", "autoscaling"]:
        value = spec.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise cli.CliError(f"{section} must be a YAML object.")
        body[section] = value
    return body


def validate_config_model_name(model_name: str) -> None:
    """Validate model_name before sending a Truss push request."""
    try:
        validate_deployment_name(model_name)
    except ValidationError as exc:
        raise cli.CliError(
            "config.yaml model_name is invalid. Use lowercase letters, numbers, "
            "and hyphens only, and start and end with a letter or number."
        ) from exc


def print_push_success(model_name: str, base_url: str, response: dict[str, Any]) -> None:
    """Print MiniTen deployment success copy."""
    model = response.get("modelDeployment") if isinstance(response, dict) else None
    project_id = (
        model.get("projectID")
        if isinstance(model, dict) and model.get("projectID")
        else "<project-id>"
    )
    deployed_name = (
        model.get("name")
        if isinstance(model, dict) and model.get("name")
        else model_name
    )
    logs_url = build_dashboard_logs_url(base_url, project_id, deployed_name)
    print(f"✨ Model {model_name} was successfully pushed ✨")
    print()
    print(f"🪵 View logs for your deployment at {logs_url}")
    print("👀 Watching for changes to truss...")


def print_update_success(
    _config_path: Path,
    client: cli.ApiClient,
    response: dict[str, Any],
) -> None:
    """Print MiniTen update success copy for a watched config change."""
    model = response.get("modelDeployment", {})
    model_name = (
        model.get("name")
        if isinstance(model, dict) and model.get("name")
        else "<model-name>"
    )
    project_id = (
        model.get("projectID")
        if isinstance(model, dict) and model.get("projectID")
        else "<project-id>"
    )
    print(
        "🪵  View logs for your deployment at "
        f"{build_dashboard_logs_url(client.state.base_url(), project_id, model_name)}"
    )


def build_dashboard_logs_url(base_url: str, project_id: str, model_name: str) -> str:
    """Build the MiniTen dashboard URL for one model's logs page."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return (
        f"{root}/projects/{quote(project_id, safe='')}"
        f"/models/{quote(model_name, safe='')}/logs"
    )


def string_field(spec: dict[str, Any], key: str) -> str | None:
    """Return a string config field, rejecting non-string values."""
    value = spec.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise cli.CliError(f"{key} must be a string.")
    return value.strip()


def build_parser() -> argparse.ArgumentParser:
    """Build the Truss-style parser."""
    parser = argparse.ArgumentParser(
        prog="truss",
        description="MiniTen Truss-style deployment wrapper.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=dedent(
            """\
            command reference:
              login [--api-key <account-api-key>] [--base-url <url>]
              init <project-name> [--model-name <deployment-name>]
              push [--config <path>] [--poll-interval <seconds>] [--no-watch]
              watch [--config <path>] [--poll-interval <seconds>]

            typical workflow:
              truss login
              truss init qwen-2.5-3b
              cd qwen-2.5-3b
              truss push

            config.yaml fields:
              model_name: MiniTen deployment name; lowercase letters, numbers, and hyphens.
              model_id: Hugging Face model ID passed to vLLM.
              replicas: Fixed replica count when autoscaling is disabled.
              resources: cpu_request, cpu_limit, memory_request, memory_limit, gpu_count.
              vllm: dtype and max_model_len.
              autoscaling: enabled, min_replicas, max_replicas, target_cpu_utilization.

            notes:
              Account API keys are used for truss control-plane automation.
              Project API keys are still used for inference requests.
              truss push watches config.yaml by default; use --no-watch to deploy once.
            """
        ),
    )
    parser.set_defaults(handler=lambda args, state, client: parser.print_help())
    subcommands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{login,init,push,watch}",
    )

    login_parser = subcommands.add_parser(
        "login",
        description="Store a MiniTen account API key for Truss-style commands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=dedent(
            """\
            examples:
              truss login
              truss login --api-key mt_account_... --base-url http://127.0.0.1:8000
            """
        ),
        help="Store a MiniTen account API key.",
    )
    login_parser.add_argument(
        "--api-key",
        metavar="<account-api-key>",
        help="MiniTen account API key. If omitted, truss prompts securely.",
    )
    login_parser.add_argument(
        "--base-url",
        metavar="<url>",
        help="MiniTen API/dashboard URL. Defaults to the configured CLI URL.",
    )
    login_parser.set_defaults(handler=login)

    init_parser = subcommands.add_parser(
        "init",
        description=(
            "Create or reuse a MiniTen project and write a starter config.yaml "
            "inside a local project directory."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=dedent(
            """\
            examples:
              truss init qwen-2.5-3b
              truss init qwen-2.5-3b --model-name qwen-2-5-3b
            """
        ),
        help="Create/reuse a project and write config.yaml.",
    )
    init_parser.add_argument(
        "name",
        metavar="<project-name>",
        help="MiniTen project name and local directory name to initialize.",
    )
    init_parser.add_argument(
        "--model-name",
        metavar="<deployment-name>",
        help="MiniTen deployment model_name written into config.yaml.",
    )
    init_parser.set_defaults(handler=init)

    push_parser = subcommands.add_parser(
        "push",
        description=(
            "Deploy the model described by config.yaml into the MiniTen project "
            "named by the current directory."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=dedent(
            """\
            examples:
              truss push
              truss push --config config.yaml
              truss push --no-watch
              truss push --poll-interval 2
            """
        ),
        help="Deploy config.yaml and watch for changes by default.",
    )
    push_parser.add_argument(
        "--config",
        metavar="<path>",
        help="Path to config.yaml. Defaults to ./config.yaml.",
    )
    push_parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        metavar="<seconds>",
        help="Seconds between config checks after push. Default: 1.0.",
    )
    push_parser.add_argument(
        "--no-watch",
        action="store_true",
        help="Deploy once and exit instead of watching for config changes.",
    )
    push_parser.set_defaults(handler=push)

    watch_parser = subcommands.add_parser(
        "watch",
        description=(
            "Watch config.yaml for changes and queue update_model jobs for the "
            "existing MiniTen deployment."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=dedent(
            """\
            examples:
              truss watch
              truss watch --config config.yaml
              truss watch --poll-interval 2
            """
        ),
        help="Watch config.yaml and queue update jobs.",
    )
    watch_parser.add_argument(
        "--config",
        metavar="<path>",
        help="Path to config.yaml. Defaults to ./config.yaml.",
    )
    watch_parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        metavar="<seconds>",
        help="Seconds between config checks. Default: 1.0.",
    )
    watch_parser.add_argument(
        "--once",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    watch_parser.set_defaults(handler=watch)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Console entrypoint for the Truss-style wrapper."""
    configure_output_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    state = cli.CliState()
    client = cli.ApiClient(state)
    try:
        args.handler(args, state, client)
    except cli.CliError as exc:
        print(f"truss: {exc}", file=sys.stderr)
        return 1
    return 0


def configure_output_streams() -> None:
    """Prefer UTF-8 output so Truss status icons work on Windows pipes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


if __name__ == "__main__":
    raise SystemExit(main())
