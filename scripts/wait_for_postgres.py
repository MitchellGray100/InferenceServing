"""Wait for the local Postgres service to accept connections."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402


def wait_for_postgres(database_url: str, *, timeout_seconds: int) -> None:
    """Poll Postgres until it accepts a simple SELECT or the timeout expires."""
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise RuntimeError("Install dependencies with `make install` first.") from exc

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with psycopg.connect(database_url, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            print("Postgres is ready.")
            return
        except Exception as exc:
            last_error = exc
            print("Waiting for Postgres...")
            time.sleep(1)

    raise RuntimeError(f"Timed out waiting for Postgres: {last_error}")


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description="Wait for local Postgres.")
    parser.add_argument(
        "--database-url",
        default=Config.DATABASE_URL,
        help="Postgres connection URL.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=60,
        help="Maximum number of seconds to wait.",
    )
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint."""
    args = parse_args()
    wait_for_postgres(args.database_url, timeout_seconds=args.timeout_seconds)


if __name__ == "__main__":
    main()
