"""Guard checks for local Makefile workflows."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETUP_MARKER = PROJECT_ROOT / ".local" / "setup-env.ok"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def assert_api_not_running(host: str, port: int) -> None:
    """Fail if the local API port is already accepting TCP connections."""
    if not is_port_open(host, port):
        return

    raise RuntimeError(
        f"Port {port} is already accepting connections. Stop `make run-api` "
        "before running `make setup-env`, then start it again afterward."
    )


def assert_setup_complete() -> None:
    """Fail if setup-env has not completed successfully."""
    if SETUP_MARKER.exists():
        return

    raise RuntimeError(
        "`make setup-env` has not completed for this checkout. Run "
        "`make setup-env` before `make run-api`."
    )


def mark_setup_complete() -> None:
    """Write the local setup marker after setup-env finishes successfully."""
    SETUP_MARKER.parent.mkdir(parents=True, exist_ok=True)
    SETUP_MARKER.write_text("ok\n", encoding="utf-8")
    print(f"Wrote setup marker: {SETUP_MARKER}")


def clear_setup_marker() -> None:
    """Remove the local setup marker during environment cleanup."""
    if SETUP_MARKER.exists():
        SETUP_MARKER.unlink()
        print(f"Removed setup marker: {SETUP_MARKER}")


def is_port_open(host: str, port: int) -> bool:
    """Return whether a TCP connection can be opened to host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def parse_args() -> argparse.Namespace:
    """Parse the guard command."""
    parser = argparse.ArgumentParser(description="Local environment guard checks.")
    parser.add_argument(
        "command",
        choices=[
            "assert-api-not-running",
            "assert-setup-complete",
            "mark-setup-complete",
            "clear-setup-marker",
        ],
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()

    try:
        if args.command == "assert-api-not-running":
            assert_api_not_running(args.host, args.port)
        elif args.command == "assert-setup-complete":
            assert_setup_complete()
        elif args.command == "mark-setup-complete":
            mark_setup_complete()
        elif args.command == "clear-setup-marker":
            clear_setup_marker()
    except RuntimeError as exc:
        print(exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
