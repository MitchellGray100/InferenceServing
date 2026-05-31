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


def test_clean_env_stops_kind_without_deleting_cache() -> None:
    """Environment cleanup should stop kind but preserve the cluster cache."""
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    clean_env = makefile.split("clean-env:", 1)[1].split("\n\n", 1)[0]

    assert "$(MAKE) stop-kind" in clean_env
    assert "clean-kind" not in clean_env


def test_setup_env_starts_existing_kind_before_ensure() -> None:
    """Setup should restart a stopped kind node before cluster validation."""
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    setup_env = makefile.split("setup-env: install", 1)[1].split("\n\n", 1)[0]

    assert "$(MAKE) start-kind" in setup_env
    assert setup_env.index("$(MAKE) start-kind") < setup_env.index("scripts/kind_env.py ensure")


def test_setup_env_installs_metrics_server_after_kind_ensure() -> None:
    """Setup should prepare metrics-server after kind is ready."""
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    setup_env = makefile.split("setup-env: install", 1)[1].split("\n\n", 1)[0]

    assert "$(MAKE) install-metrics-server" in setup_env
    assert setup_env.index("scripts/kind_env.py ensure") < setup_env.index(
        "$(MAKE) install-metrics-server"
    )


def test_real_kubernetes_worker_installs_metrics_server() -> None:
    """Real Kubernetes worker startup should prepare metrics-server for HPA."""
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    start_worker = makefile.split("start-worker-real-k8s:", 1)[1].split("\n\n", 1)[0]

    assert "$(MAKE) install-metrics-server" in start_worker
    assert start_worker.index("scripts/kind_env.py ensure") < start_worker.index(
        "$(MAKE) install-metrics-server"
    )


def test_local_api_smoke_forces_dry_run_worker() -> None:
    """API smoke tests should not inherit a real Kubernetes worker from prior smoke tests."""
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    test_local_apis = makefile.split("test-local-apis:", 1)[1].split("\n\n", 1)[0]

    assert "WORKER_DRY_RUN=true" in test_local_apis
    assert "docker compose up -d --build --force-recreate" in test_local_apis
    assert test_local_apis.index("WORKER_DRY_RUN=true") < test_local_apis.index(
        "scripts/smoke_test_local_api.py"
    )


def test_compose_worker_defaults_to_two_replicas() -> None:
    """Local Compose worker startup should run two deployment workers."""
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "WORKER_REPLICAS ?= 2" in makefile
    assert "--scale worker=$(WORKER_REPLICAS) worker" in makefile
