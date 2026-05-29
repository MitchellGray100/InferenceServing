"""Runtime entrypoint tests."""

import signal
import subprocess

from flask import Flask

from scripts import start_dashboard
from scripts import stop_local_api
import wsgi
from app import main as app_main
from app.services import deployment_worker
from scripts import check_local_gpu_k8s
from scripts import kind_env


def test_wsgi_exposes_flask_app() -> None:
    """Gunicorn can import the WSGI application object."""
    assert isinstance(wsgi.app, Flask)


def test_app_main_runs_configured_flask_server(monkeypatch) -> None:
    """The local API entrypoint passes Config host/port into Flask.run."""
    captured = {}

    monkeypatch.setattr(app_main.Config, "API_HOST", "127.0.0.1")
    monkeypatch.setattr(app_main.Config, "API_PORT", 9999)
    monkeypatch.setattr(app_main.Config, "API_DEBUG", True)
    monkeypatch.setattr(app_main.Config, "API_RELOAD", False)
    monkeypatch.setattr(
        app_main.app,
        "run",
        lambda **kwargs: captured.update(kwargs),
    )

    app_main.main()

    assert captured == {
        "host": "127.0.0.1",
        "port": 9999,
        "debug": True,
        "use_reloader": False,
    }


def test_worker_run_forever_stops_immediately() -> None:
    """A pre-set shutdown callback exits the worker loop before polling."""
    deployment_worker.run_forever(
        clients=object(),
        config=deployment_worker.WorkerConfig(worker_id="worker-1"),
        should_stop=lambda: True,
    )


def test_worker_run_forever_sleeps_when_no_work(monkeypatch) -> None:
    """The worker sleeps between empty polling iterations."""
    stopped = {"value": False}

    monkeypatch.setattr(
        deployment_worker,
        "process_next_job",
        lambda clients, worker_id: deployment_worker.JobResult(processed=False),
    )

    def sleep(seconds):
        assert seconds == 0.01
        stopped["value"] = True

    monkeypatch.setattr(deployment_worker.time, "sleep", sleep)

    deployment_worker.run_forever(
        clients=object(),
        config=deployment_worker.WorkerConfig(
            worker_id="worker-1",
            poll_interval_seconds=0.01,
        ),
        should_stop=lambda: stopped["value"],
    )

    assert stopped["value"] is True


def test_worker_setup_logging(monkeypatch) -> None:
    """Worker logging setup respects the LOG_LEVEL environment variable."""
    captured = {}

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(
        deployment_worker.logging,
        "basicConfig",
        lambda **kwargs: captured.update(kwargs),
    )

    deployment_worker.setup_logging()

    assert captured["level"] == "DEBUG"
    assert "%(levelname)s" in captured["format"]


def test_worker_build_shutdown_event_registers_signal_handlers(monkeypatch) -> None:
    """SIGINT/SIGTERM handlers set the worker shutdown event."""
    registered = {}

    def fake_signal(signum, handler):
        registered[signum] = handler

    monkeypatch.setattr(deployment_worker.signal, "signal", fake_signal)

    event = deployment_worker.build_shutdown_event()
    assert event.is_set() is False

    registered[signal.SIGTERM](signal.SIGTERM, None)

    assert event.is_set() is True
    assert signal.SIGINT in registered
    assert signal.SIGTERM in registered


def test_worker_main_wires_logging_signal_and_loop(monkeypatch) -> None:
    """The worker CLI builds shutdown wiring before entering the poll loop."""
    calls = []

    monkeypatch.setattr(
        deployment_worker,
        "setup_logging",
        lambda: calls.append("logging"),
    )
    monkeypatch.setattr(
        deployment_worker,
        "build_shutdown_event",
        lambda: FakeStopEvent(calls),
    )
    monkeypatch.setattr(
        deployment_worker,
        "run_forever",
        lambda should_stop: calls.append(("run", should_stop())),
    )

    deployment_worker.main()

    assert calls == ["logging", "event", ("run", True)]


def test_kind_env_exports_docker_kubeconfig(monkeypatch, tmp_path) -> None:
    """kind kubeconfig is exported unchanged for host-networked Compose workers."""
    kube_dir = tmp_path / "kube"
    monkeypatch.setattr(kind_env, "LOCAL_KUBE_DIR", kube_dir)
    monkeypatch.setattr(kind_env, "LOCAL_KUBECONFIG", kube_dir / "config")

    def fake_run(args, capture=False):
        assert args == ["kind", "get", "kubeconfig", "--name", "miniten"]
        assert capture is True
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="clusters:\n- cluster:\n    server: https://127.0.0.1:12345\n",
        )

    monkeypatch.setattr(kind_env, "run", fake_run)

    kind_env.export_docker_kubeconfig("miniten")

    assert "server: https://127.0.0.1:12345" in (
        kube_dir / "config"
    ).read_text(encoding="utf-8")


def test_kind_env_delete_skips_missing_kind(monkeypatch, tmp_path) -> None:
    """Cleanup should still remove generated kubeconfig if kind is unavailable."""
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("old", encoding="utf-8")
    monkeypatch.setattr(kind_env, "LOCAL_KUBECONFIG", kubeconfig)
    monkeypatch.setattr(kind_env.shutil, "which", lambda name: None)

    kind_env.delete_kind_environment("miniten")

    assert kubeconfig.exists() is False


def test_kind_env_gpu_ensure_runs_gpu_setup(monkeypatch) -> None:
    """GPU kind setup verifies Docker GPU access before configuring the node."""
    calls = []

    monkeypatch.setattr(kind_env, "require_tool", lambda name: calls.append(("tool", name)))
    monkeypatch.setattr(kind_env, "existing_clusters", lambda: {"miniten"})
    monkeypatch.setattr(kind_env, "export_docker_kubeconfig", lambda name: calls.append(("kubeconfig", name)))
    monkeypatch.setattr(kind_env, "verify_docker_gpu_runtime", lambda: calls.append("gpu-runtime"))
    monkeypatch.setattr(kind_env, "ensure_kind_gpu_support", lambda name: calls.append(("gpu-kind", name)))
    monkeypatch.setattr(kind_env, "run", lambda args, capture=False: calls.append(tuple(args)))

    kind_env.ensure_kind_environment("miniten", gpu=True)

    assert "gpu-runtime" in calls
    assert ("gpu-kind", "miniten") in calls


def test_kind_env_recreates_unusable_existing_cluster(monkeypatch) -> None:
    """A stale kind container without an API port mapping is recreated."""
    calls = []
    exports = {"count": 0}

    monkeypatch.setattr(kind_env, "require_tool", lambda name: calls.append(("tool", name)))
    monkeypatch.setattr(kind_env, "existing_clusters", lambda: {"miniten"})

    def fake_export(cluster_name):
        exports["count"] += 1
        calls.append(("export", cluster_name))
        if exports["count"] == 1:
            raise RuntimeError("failed to get api server port")

    def fake_run(args, capture=False):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(kind_env, "export_docker_kubeconfig", fake_export)
    monkeypatch.setattr(kind_env, "run", fake_run)

    kind_env.ensure_kind_environment("miniten")

    assert ("kind", "delete", "cluster", "--name", "miniten") in calls
    assert ("kind", "create", "cluster", "--name", "miniten") in calls
    assert calls.count(("export", "miniten")) == 2


def test_kind_env_wait_for_allocatable_gpus(monkeypatch) -> None:
    """GPU kind setup waits until the device plugin publishes nvidia.com/gpu."""
    calls = []

    def fake_run(args, capture=False):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="1")

    monkeypatch.setattr(kind_env, "run", fake_run)

    kind_env.wait_for_allocatable_gpus("miniten")

    assert calls


def test_kind_env_patches_local_gpu_capacity(monkeypatch) -> None:
    """Local WSL2 GPU setup can publish a schedulable GPU resource."""
    captured = {}

    def fake_run(args, capture=False):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(kind_env, "run", fake_run)

    kind_env.patch_node_gpu_capacity("miniten", "miniten-control-plane")

    assert captured["args"][:3] == ["kubectl", "patch", "node"]
    assert "nvidia.com/gpu" in captured["args"][-1]


def test_gpu_preflight_counts_allocatable_gpu_nodes() -> None:
    """GPU smoke preflight reads the NVIDIA extended resource."""
    payload = {
        "items": [
            {
                "metadata": {"name": "node-a"},
                "status": {"allocatable": {"nvidia.com/gpu": "1"}},
            },
            {
                "metadata": {"name": "node-b"},
                "status": {"allocatable": {"cpu": "8"}},
            },
        ]
    }

    assert check_local_gpu_k8s.gpu_nodes(payload) == [("node-a", 1)]


def test_gpu_preflight_parses_compute_capabilities() -> None:
    """GPU preflight reads Docker-visible compute capability output."""
    output = "NVIDIA GeForce GTX 1080 Ti, 6.1\nNVIDIA T4, 7.5\n"

    assert check_local_gpu_k8s.docker_gpu_compute_capabilities(output) == [
        ("NVIDIA GeForce GTX 1080 Ti", "6.1"),
        ("NVIDIA T4", "7.5"),
    ]


def test_gpu_preflight_rejects_old_vllm_gpu(monkeypatch, capsys) -> None:
    """Current vLLM GPU images should fail early on Pascal-era GPUs."""
    monkeypatch.setenv("MINITEN_VLLM_GPU_MIN_COMPUTE_CAPABILITY", "7.5")

    assert (
        check_local_gpu_k8s.validate_vllm_gpu_compute_capability(
            [("NVIDIA GeForce GTX 1080 Ti", "6.1")]
        )
        is False
    )
    assert "1080 Ti" in capsys.readouterr().err


def test_gpu_preflight_accepts_supported_vllm_gpu(monkeypatch) -> None:
    """Current vLLM GPU images can proceed on supported GPU generations."""
    monkeypatch.setenv("MINITEN_VLLM_GPU_MIN_COMPUTE_CAPABILITY", "7.5")

    assert (
        check_local_gpu_k8s.validate_vllm_gpu_compute_capability(
            [("NVIDIA T4", "7.5")]
        )
        is True
    )


def test_gpu_preflight_rejects_kind_on_docker_desktop(monkeypatch, capsys) -> None:
    """Docker Desktop kind can schedule fake GPUs but cannot run CUDA pods."""
    monkeypatch.setattr(check_local_gpu_k8s, "current_kubernetes_context", lambda: "kind-miniten")
    monkeypatch.setattr(check_local_gpu_k8s, "docker_operating_system", lambda: "Docker Desktop")

    assert check_local_gpu_k8s.validate_supported_gpu_kubernetes_backend() is False
    assert "kind on Docker Desktop" in capsys.readouterr().err


def test_gpu_preflight_accepts_non_kind_backend(monkeypatch) -> None:
    """GPU smoke can proceed against a non-kind GPU-capable cluster."""
    monkeypatch.setattr(check_local_gpu_k8s, "current_kubernetes_context", lambda: "minikube")
    monkeypatch.setattr(check_local_gpu_k8s, "docker_operating_system", lambda: "Docker Desktop")

    assert check_local_gpu_k8s.validate_supported_gpu_kubernetes_backend() is True


def test_gpu_preflight_main_fails_without_allocatable_gpus(monkeypatch, capsys) -> None:
    """GPU smoke preflight fails before creating unschedulable vLLM pods."""
    monkeypatch.setattr(
        check_local_gpu_k8s,
        "validate_supported_gpu_kubernetes_backend",
        lambda: True,
    )
    monkeypatch.setattr(
        check_local_gpu_k8s,
        "run_docker_gpu_query",
        lambda: "NVIDIA T4, 7.5\n",
    )
    monkeypatch.setattr(
        check_local_gpu_k8s,
        "run_kubectl_json",
        lambda args: {"items": [{"metadata": {"name": "node-a"}, "status": {}}]},
    )

    assert check_local_gpu_k8s.main() == 1
    assert "nvidia.com/gpu" in capsys.readouterr().err


def test_start_dashboard_opens_existing_server(monkeypatch) -> None:
    """Dashboard launcher opens the browser if the API is already running."""
    calls = []

    monkeypatch.setattr(start_dashboard, "assert_setup_complete", lambda: calls.append("setup"))
    monkeypatch.setattr(start_dashboard, "is_port_open", lambda host, port: True)
    monkeypatch.setattr(start_dashboard.webbrowser, "open", lambda url: calls.append(("open", url)))
    monkeypatch.setattr(
        start_dashboard,
        "start_api_process",
        lambda: calls.append("start"),
    )

    assert start_dashboard.main() == 0
    assert "setup" in calls
    assert "start" not in calls
    assert ("open", start_dashboard.DASHBOARD_URL) in calls


def test_start_dashboard_starts_server_when_port_is_closed(monkeypatch) -> None:
    """Dashboard launcher starts Flask before opening the browser."""
    calls = []

    monkeypatch.setattr(start_dashboard, "assert_setup_complete", lambda: calls.append("setup"))
    monkeypatch.setattr(start_dashboard, "is_port_open", lambda host, port: False)
    monkeypatch.setattr(start_dashboard, "start_api_process", lambda: calls.append("start"))
    monkeypatch.setattr(start_dashboard, "wait_for_dashboard", lambda: calls.append("wait"))
    monkeypatch.setattr(start_dashboard.webbrowser, "open", lambda url: calls.append(("open", url)))

    assert start_dashboard.main() == 0
    assert calls == ["setup", "start", "wait", ("open", start_dashboard.DASHBOARD_URL)]


def test_stop_local_api_matches_app_main_processes(monkeypatch) -> None:
    """Cleanup only targets the local Flask API entrypoint."""
    monkeypatch.setattr(stop_local_api.os, "getpid", lambda: 10)
    monkeypatch.setattr(stop_local_api.os, "getppid", lambda: 9)

    processes = [
        stop_local_api.LocalProcess(1, "python -m app.main"),
        stop_local_api.LocalProcess(2, "python -m app.services.deployment_worker"),
        stop_local_api.LocalProcess(10, "python -m app.main"),
        stop_local_api.LocalProcess(11, "python scripts/stop_local_api.py"),
    ]

    assert stop_local_api.matching_api_processes(processes) == [
        stop_local_api.LocalProcess(1, "python -m app.main")
    ]


def test_stop_local_api_stops_matching_processes(monkeypatch) -> None:
    """The cleanup entrypoint terminates every matching API process."""
    stopped = []
    processes = [
        stop_local_api.LocalProcess(123, "python -m app.main"),
        stop_local_api.LocalProcess(456, "python other.py"),
    ]

    monkeypatch.setattr(stop_local_api.os, "getpid", lambda: 1)
    monkeypatch.setattr(stop_local_api.os, "getppid", lambda: 2)
    monkeypatch.setattr(stop_local_api, "list_processes", lambda: processes)
    monkeypatch.setattr(stop_local_api, "stop_process", lambda process: stopped.append(process.pid))

    assert stop_local_api.stop_local_api_processes() == 1
    assert stopped == [123]


class FakeStopEvent:
    def __init__(self, calls):
        calls.append("event")

    def is_set(self) -> bool:
        return True
