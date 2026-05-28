"""Raw SQL migration runner.

MiniTen uses explicit `.sql` migration files instead of an ORM migration
framework. This module applies those files in filename order and records each
applied file in `schema_migrations` so the same migration is not run twice.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Config


MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  checksum TEXT NOT NULL,
  applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    """A migration file plus its immutable content checksum."""

    version: str
    path: Path
    sql: str
    checksum: str


def project_root() -> Path:
    """Return the repository root directory."""
    return Path(__file__).resolve().parents[2]


def migrations_dir() -> Path:
    """Return the default directory containing SQL migration files."""
    return project_root() / "migrations"


def load_dotenv_file(path: Path | None = None) -> None:
    """Load a simple .env file without overriding existing environment values."""
    dotenv_path = path or project_root() / ".env"
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        # This is intentionally small: it supports KEY=value lines used by the
        # local app without pulling in an extra runtime dependency.
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        # Preserve any value already exported by the shell; .env only fills
        # missing variables for local development.
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_migrations(directory: Path) -> list[Migration]:
    """Read all `.sql` migrations from a directory in deterministic order."""
    migrations: list[Migration] = []

    for path in sorted(directory.glob("*.sql")):
        # The checksum lets us detect a migration file that was edited after it
        # had already been applied to a database.
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        migrations.append(
            Migration(
                version=path.name,
                path=path,
                sql=sql,
                checksum=checksum,
            )
        )

    return migrations


def get_applied_migrations(conn: Any) -> dict[str, str]:
    """Return already-applied migrations keyed by filename."""
    with conn.cursor() as cur:
        # Ensure the bookkeeping table exists before checking which migrations
        # have already run.
        cur.execute(MIGRATION_TABLE_SQL)
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return dict(cur.fetchall())


def apply_migration(conn: Any, migration: Migration) -> None:
    """Apply one migration and record its checksum atomically."""
    # If the migration SQL fails, the schema_migrations insert rolls back too,
    # so the database will retry the same migration on the next run.
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(migration.sql)
            cur.execute(
                """
                INSERT INTO schema_migrations (version, checksum)
                VALUES (%s, %s)
                """,
                (migration.version, migration.checksum),
            )


def migrate(database_url: str, directory: Path | None = None) -> list[str]:
    """Apply pending migrations and return the filenames applied in this run."""
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "psycopg is required to run migrations. Install project dependencies "
            "with `make install`."
        ) from exc

    directory = directory or migrations_dir()
    migrations = load_migrations(directory)
    applied_now: list[str] = []
    logger.info("Starting database migration run directory=%s.", directory)

    with psycopg.connect(database_url) as conn:
        applied = get_applied_migrations(conn)

        for migration in migrations:
            # Matching checksum means this migration already ran unchanged.
            existing_checksum = applied.get(migration.version)

            if existing_checksum == migration.checksum:
                continue

            if existing_checksum is not None:
                # Editing applied migrations can make environments diverge, so
                # fail loudly instead of guessing how to recover.
                raise RuntimeError(
                    "Applied migration checksum mismatch for "
                    f"{migration.version}. Refusing to continue."
                )

            apply_migration(conn, migration)
            applied_now.append(migration.version)
            logger.info("Applied database migration version=%s.", migration.version)

    logger.info("Finished database migration run applied_count=%s.", len(applied_now))
    return applied_now


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the migration runner."""
    parser = argparse.ArgumentParser(description="Apply MiniTen database migrations.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres connection URL. Defaults to DATABASE_URL.",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=None,
        help="Directory containing .sql migration files.",
    )
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint used by `python -m app.db.migrate`."""
    load_dotenv_file()
    args = parse_args()
    database_url = args.database_url or os.getenv("DATABASE_URL") or Config.DATABASE_URL

    applied = migrate(database_url=database_url, directory=args.migrations_dir)

    if not applied:
        print("No migrations to apply.")
        return

    for version in applied:
        print(f"Applied migration: {version}")


if __name__ == "__main__":
    main()
