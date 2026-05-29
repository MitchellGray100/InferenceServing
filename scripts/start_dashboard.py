"""Start the local MiniTen API/dashboard and open it in a browser."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

try:
    from local_env_guard import (
        DEFAULT_HOST,
        DEFAULT_PORT,
        assert_setup_complete,
        is_port_open,
    )
except ModuleNotFoundError:
    from scripts.local_env_guard import (
        DEFAULT_HOST,
        DEFAULT_PORT,
        assert_setup_complete,
        is_port_open,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / ".local" / "logs"
DASHBOARD_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def start_api_process() -> None:
    """Start Flask in the background with logs under .local/logs."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("API_DEBUG", "true")
    env["API_RELOAD"] = "false"
    env["PYTHONUNBUFFERED"] = "1"

    stdout = (LOG_DIR / "api.out.log").open("ab")
    stderr = (LOG_DIR / "api.err.log").open("ab")
    kwargs: dict[str, object] = {
        "cwd": PROJECT_ROOT,
        "env": env,
        "stdout": stdout,
        "stderr": stderr,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen([sys.executable, "-m", "app.main"], **kwargs)


def wait_for_dashboard() -> None:
    """Wait until the dashboard home page returns a response."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(DASHBOARD_URL, timeout=2) as response:
                if response.status < 500:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    raise RuntimeError(
        f"Timed out waiting for {DASHBOARD_URL}. "
        f"Check logs in {LOG_DIR}."
    )


def main() -> int:
    """CLI entrypoint."""
    try:
        assert_setup_complete()
        if is_port_open(DEFAULT_HOST, DEFAULT_PORT):
            print(f"MiniTen dashboard is already running at {DASHBOARD_URL}")
        else:
            print(f"Starting MiniTen dashboard at {DASHBOARD_URL}")
            start_api_process()
            wait_for_dashboard()
        webbrowser.open(DASHBOARD_URL)
        print(f"Opened {DASHBOARD_URL}")
    except RuntimeError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
