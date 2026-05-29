from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_setup_web_starts_real_kubernetes_worker() -> None:
    """The website setup should run with WORKER_DRY_RUN=false."""
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    setup_web = makefile.split("setup-web: setup-env", 1)[1].split("\n\n", 1)[0]

    assert "$(MAKE) start-worker-real-k8s" in setup_web


def test_clean_env_stops_local_api_before_compose_cleanup() -> None:
    """Environment cleanup should stop background dashboard/API processes."""
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    clean_env = makefile.split("clean-env:", 1)[1].split("\n\n", 1)[0]

    assert "scripts/stop_local_api.py" in clean_env
    assert clean_env.index("scripts/stop_local_api.py") < clean_env.index("docker compose down")
