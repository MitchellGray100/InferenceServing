"""Runtime entrypoint tests."""

import signal

from flask import Flask

import wsgi
from app import main as app_main
from app.services import deployment_worker


def test_wsgi_exposes_flask_app() -> None:
    """Gunicorn can import the WSGI application object."""
    assert isinstance(wsgi.app, Flask)


def test_app_main_runs_configured_flask_server(monkeypatch) -> None:
    """The local API entrypoint passes Config host/port into Flask.run."""
    captured = {}

    monkeypatch.setattr(app_main.Config, "API_HOST", "127.0.0.1")
    monkeypatch.setattr(app_main.Config, "API_PORT", 9999)
    monkeypatch.setattr(app_main.Config, "API_DEBUG", True)
    monkeypatch.setattr(
        app_main.app,
        "run",
        lambda **kwargs: captured.update(kwargs),
    )

    app_main.main()

    assert captured == {"host": "127.0.0.1", "port": 9999, "debug": True}


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


class FakeStopEvent:
    def __init__(self, calls):
        calls.append("event")

    def is_set(self) -> bool:
        return True
