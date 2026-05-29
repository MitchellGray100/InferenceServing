"""Database migration and pool infrastructure tests."""

from contextlib import contextmanager
import sys

import pytest

from app.db import migrate, pool


def test_project_root_and_migrations_dir_are_repo_paths() -> None:
    root = migrate.project_root()

    assert root.name == "InferenceServing"
    assert migrate.migrations_dir() == root / "migrations"


def test_load_dotenv_file_sets_missing_values_only(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
        # comment
        DATABASE_URL=postgresql://example
        EXISTING='from-file'
        QUOTED="hello"
        INVALID_LINE
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("EXISTING", "already-set")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)

    migrate.load_dotenv_file(env_file)

    assert migrate.os.environ["DATABASE_URL"] == "postgresql://example"
    assert migrate.os.environ["EXISTING"] == "already-set"
    assert migrate.os.environ["QUOTED"] == "hello"


def test_load_dotenv_file_ignores_missing_file(tmp_path) -> None:
    migrate.load_dotenv_file(tmp_path / "missing.env")


def test_load_migrations_returns_sorted_checksummed_files(tmp_path) -> None:
    (tmp_path / "002_second.sql").write_text("SELECT 2;", encoding="utf-8")
    (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "README.md").write_text("ignore me", encoding="utf-8")

    migrations = migrate.load_migrations(tmp_path)

    assert [item.version for item in migrations] == [
        "001_first.sql",
        "002_second.sql",
    ]
    assert all(len(item.checksum) == 64 for item in migrations)


def test_schema_enforces_and_names_unique_api_key_hash() -> None:
    """Regression test for the DB-level duplicate API key guard."""
    initial_schema = (migrate.migrations_dir() / "001_initial_schema.sql").read_text(
        encoding="utf-8"
    )
    api_key_name_migration = (
        migrate.migrations_dir() / "002_api_key_active_name_uniqueness.sql"
    ).read_text(
        encoding="utf-8"
    )

    assert "CONSTRAINT uq_api_keys_key_hash UNIQUE(key_hash)" in initial_schema
    assert "DROP CONSTRAINT IF EXISTS uq_api_keys_project_name" in api_key_name_migration
    assert "CREATE UNIQUE INDEX uq_api_keys_project_active_name" in api_key_name_migration
    assert "WHERE revoked_at IS NULL" in api_key_name_migration


def test_model_deployment_names_are_reserved_until_worker_marks_deleted() -> None:
    """Deleting reserves a model name until Kubernetes cleanup completes."""
    initial_schema = (migrate.migrations_dir() / "001_initial_schema.sql").read_text(
        encoding="utf-8"
    )
    deployment_queries = (
        migrate.project_root() / "app" / "db" / "queries" / "model_deployments.sql"
    ).read_text(
        encoding="utf-8"
    )
    delete_requested = deployment_queries.split(
        "-- name: advance_model_deployment_delete_requested",
        maxsplit=1,
    )[1].split("-- name: advance_model_deployment_replicas", maxsplit=1)[0]
    mark_deleted = deployment_queries.split(
        "-- name: mark_model_deployment_deleted",
        maxsplit=1,
    )[1]

    assert "CREATE UNIQUE INDEX uq_model_deployments_active_project_name" in initial_schema
    assert "WHERE deleted_at IS NULL" in initial_schema
    assert "status = 'deleting'" in delete_requested
    assert "deleted_at = CURRENT_TIMESTAMP" not in delete_requested
    assert "name = name || '-deleted-'" not in delete_requested
    assert "status = 'deleted'" in mark_deleted
    assert "deleted_at = COALESCE(deleted_at, CURRENT_TIMESTAMP)" in mark_deleted


def test_hard_restart_job_type_is_allowed_by_schema_and_migration() -> None:
    initial_schema = (migrate.migrations_dir() / "001_initial_schema.sql").read_text(
        encoding="utf-8"
    )
    hard_restart_migration = (
        migrate.migrations_dir() / "004_hard_restart_deployment_jobs.sql"
    ).read_text(
        encoding="utf-8"
    )

    assert "'hard_restart_model'" in initial_schema
    assert "DROP CONSTRAINT IF EXISTS deployment_jobs_job_type_check" in hard_restart_migration
    assert "'hard_restart_model'" in hard_restart_migration


def test_auth_token_version_and_hard_restart_event_schema() -> None:
    initial_schema = (migrate.migrations_dir() / "001_initial_schema.sql").read_text(
        encoding="utf-8"
    )
    migration = (
        migrate.migrations_dir() / "005_auth_token_version_and_hard_restart_event.sql"
    ).read_text(
        encoding="utf-8"
    )

    assert "token_version INTEGER NOT NULL DEFAULT 0" in initial_schema
    assert "'model_hard_restarted'" in initial_schema
    assert "ADD COLUMN IF NOT EXISTS token_version" in migration
    assert "DROP CONSTRAINT IF EXISTS model_events_event_type_check" in migration
    assert "'model_hard_restarted'" in migration


def test_get_applied_migrations_creates_table_and_returns_mapping() -> None:
    conn = FakeMigrationConnection(fetchall=[("001.sql", "abc")])

    applied = migrate.get_applied_migrations(conn)

    assert applied == {"001.sql": "abc"}
    assert conn.cursor_obj.executed[0][0] == migrate.MIGRATION_TABLE_SQL


def test_apply_migration_executes_sql_and_records_checksum(tmp_path) -> None:
    migration = migrate.Migration(
        version="001.sql",
        path=tmp_path / "001.sql",
        sql="CREATE TABLE example();",
        checksum="checksum",
    )
    conn = FakeMigrationConnection()

    migrate.apply_migration(conn, migration)

    assert conn.transaction_entered is True
    assert conn.cursor_obj.executed[0][0] == "CREATE TABLE example();"
    assert conn.cursor_obj.executed[1][1] == ("001.sql", "checksum")


def test_migrate_skips_matching_checksum(monkeypatch, tmp_path) -> None:
    migration = migrate.Migration("001.sql", tmp_path / "001.sql", "SELECT 1;", "abc")
    conn = FakePsycopgConnection(applied={"001.sql": "abc"})
    fake_psycopg = FakePsycopg(conn)
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setattr(migrate, "load_migrations", lambda directory: [migration])
    monkeypatch.setattr(migrate, "get_applied_migrations", lambda conn: {"001.sql": "abc"})

    applied = migrate.migrate("postgresql://db", tmp_path)

    assert applied == []


def test_migrate_rejects_checksum_mismatch(monkeypatch, tmp_path) -> None:
    migration = migrate.Migration("001.sql", tmp_path / "001.sql", "SELECT 1;", "new")
    monkeypatch.setattr(migrate, "load_migrations", lambda directory: [migration])
    monkeypatch.setattr(migrate, "get_applied_migrations", lambda conn: {"001.sql": "old"})
    monkeypatch.setitem(sys.modules, "psycopg", FakePsycopg(FakePsycopgConnection()))

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        migrate.migrate("postgresql://db", tmp_path)


def test_migrate_applies_pending_migration(monkeypatch, tmp_path) -> None:
    migration = migrate.Migration("001.sql", tmp_path / "001.sql", "SELECT 1;", "abc")
    monkeypatch.setattr(migrate, "load_migrations", lambda directory: [migration])
    monkeypatch.setattr(migrate, "get_applied_migrations", lambda conn: {})
    applied = []
    monkeypatch.setattr(
        migrate,
        "apply_migration",
        lambda conn, migration: applied.append(migration.version),
    )
    monkeypatch.setitem(sys.modules, "psycopg", FakePsycopg(FakePsycopgConnection()))

    assert migrate.migrate("postgresql://db", tmp_path) == ["001.sql"]
    assert applied == ["001.sql"]


def test_parse_args_reads_cli_values(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate",
            "--database-url",
            "postgresql://example",
            "--migrations-dir",
            str(tmp_path),
        ],
    )

    args = migrate.parse_args()

    assert args.database_url == "postgresql://example"
    assert args.migrations_dir == tmp_path


def test_main_prints_no_migrations(monkeypatch, capsys) -> None:
    monkeypatch.setattr(migrate, "load_dotenv_file", lambda: None)
    monkeypatch.setattr(migrate, "parse_args", lambda: Args())
    monkeypatch.setattr(migrate, "migrate", lambda database_url, directory: [])

    migrate.main()

    assert "No migrations to apply." in capsys.readouterr().out


def test_main_prints_applied_migrations(monkeypatch, capsys) -> None:
    monkeypatch.setattr(migrate, "load_dotenv_file", lambda: None)
    monkeypatch.setattr(migrate, "parse_args", lambda: Args())
    monkeypatch.setattr(
        migrate,
        "migrate",
        lambda database_url, directory: ["001.sql", "002.sql"],
    )

    migrate.main()

    output = capsys.readouterr().out
    assert "Applied migration: 001.sql" in output
    assert "Applied migration: 002.sql" in output


def test_pool_init_get_close_lifecycle(monkeypatch) -> None:
    fake_pool = FakePool()
    monkeypatch.setattr(pool, "_pool", None)
    monkeypatch.setattr(pool, "create_pool", lambda database_url=None, **kwargs: fake_pool)

    assert pool.init_pool("postgresql://db") is fake_pool
    assert fake_pool.opened is True
    assert pool.get_pool() is fake_pool

    pool.close_pool()

    assert fake_pool.closed is True
    assert pool._pool is None


def test_pool_close_is_noop_when_uninitialized(monkeypatch) -> None:
    monkeypatch.setattr(pool, "_pool", None)

    pool.close_pool()

    assert pool._pool is None


def test_pool_connection_and_transaction_contexts(monkeypatch) -> None:
    fake_pool = FakePool()
    monkeypatch.setattr(pool, "_pool", fake_pool)

    with pool.connection() as conn:
        assert conn is fake_pool.conn

    with pool.transaction() as conn:
        assert conn is fake_pool.conn

    assert fake_pool.conn.transaction_entered is True


class FakeMigrationCursor:
    def __init__(self, fetchall=None):
        self.executed = []
        self._fetchall = fetchall or []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._fetchall


class FakeMigrationConnection:
    def __init__(self, fetchall=None):
        self.cursor_obj = FakeMigrationCursor(fetchall)
        self.transaction_entered = False

    @contextmanager
    def cursor(self):
        yield self.cursor_obj

    @contextmanager
    def transaction(self):
        self.transaction_entered = True
        yield


class FakePsycopgConnection:
    def __init__(self, applied=None):
        self.applied = applied or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakePsycopg:
    def __init__(self, conn):
        self.conn = conn

    def connect(self, database_url):
        return self.conn


class FakePoolConnection:
    def __init__(self):
        self.transaction_entered = False

    @contextmanager
    def transaction(self):
        self.transaction_entered = True
        yield


class FakePool:
    def __init__(self):
        self.opened = False
        self.closed = False
        self.conn = FakePoolConnection()

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    @contextmanager
    def connection(self):
        yield self.conn


class Args:
    database_url = "postgresql://db"
    migrations_dir = None
