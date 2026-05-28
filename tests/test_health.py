"""Health and readiness route tests."""

from contextlib import contextmanager

from app import create_app


class TestConfig:
    TESTING = True
    SECRET_KEY = "test-secret-key-change-me-32-bytes"


def test_healthz_returns_ok() -> None:
    """Liveness does not require dependency checks."""
    app = create_app(TestConfig)
    client = app.test_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_readyz_returns_ready_when_postgres_check_passes(monkeypatch) -> None:
    """Readiness returns 200 when the DB check succeeds."""
    app = create_app(TestConfig)
    client = app.test_client()

    monkeypatch.setattr("app.routes.health.check_postgres", lambda: None)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ready", "checks": {"postgres": "ok"}}


def test_readyz_returns_503_when_postgres_check_fails(monkeypatch) -> None:
    """Readiness returns 503 when Postgres cannot be queried."""
    app = create_app(TestConfig)
    client = app.test_client()

    def fail_check() -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.routes.health.check_postgres", fail_check)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "not_ready",
        "checks": {"postgres": "error"},
    }


def test_check_postgres_runs_select_one(monkeypatch) -> None:
    """The readiness DB check borrows a connection and executes SELECT 1."""
    from app.routes import health

    fake = FakeConnection()
    monkeypatch.setattr(health, "connection", fake.connection)

    health.check_postgres()

    assert fake.cursor_obj.executed == ["SELECT 1"]


class FakeCursor:
    def __init__(self) -> None:
        self.executed = []

    def execute(self, sql):
        self.executed.append(sql)

    def fetchone(self):
        return {"?column?": 1}


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()

    @contextmanager
    def connection(self):
        yield self

    @contextmanager
    def cursor(self):
        yield self.cursor_obj
