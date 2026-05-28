"""Postgres connection pool management.

This module will create and expose the psycopg connection pool used by Flask
requests, background workers, and reconciliation jobs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.config import Config


_pool: Any | None = None
logger = logging.getLogger(__name__)


def create_pool(database_url: str | None = None, **kwargs: Any) -> Any:
    """Create a psycopg connection pool.

    `psycopg_pool` is imported lazily so command-line tools like `--help` can
    run before local dependencies are installed.
    """
    try:
        from psycopg_pool import ConnectionPool
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "psycopg_pool is required for database access. Install project "
            "dependencies with `make install`."
        ) from exc

    # Prefer an explicit URL for tests/scripts, otherwise use application
    # configuration. Each process owns its own pool.
    conninfo = database_url or Config.DATABASE_URL

    # psycopg can return rows as dictionaries, which keeps service code
    # readable and avoids tuple-index coupling to SELECT order.
    connection_kwargs = kwargs.pop("kwargs", {})
    connection_kwargs.setdefault("row_factory", dict_row)

    # Validate connections before handing them to request/service code. This
    # matters in local Docker workflows where Postgres may restart while the
    # Flask process is still running; without a check the pool can hand out a
    # stale socket and the next request fails with OperationalError.
    kwargs.setdefault("check", ConnectionPool.check_connection)

    min_size = kwargs.pop("min_size", 1)
    max_size = kwargs.pop("max_size", 10)
    open_pool = kwargs.pop("open", False)
    logger.info(
        "Creating Postgres connection pool min_size=%s max_size=%s open=%s.",
        min_size,
        max_size,
        open_pool,
    )

    return ConnectionPool(
        conninfo=conninfo,
        min_size=min_size,
        max_size=max_size,
        open=open_pool,
        kwargs=connection_kwargs,
        **kwargs,
    )


def init_pool(database_url: str | None = None, **kwargs: Any) -> Any:
    """Initialize and open the process-wide database pool."""
    global _pool

    # Reuse the pool inside one app/worker process. Horizontal scaling creates
    # more independent processes, each with its own bounded pool.
    if _pool is not None:
        return _pool

    _pool = create_pool(database_url=database_url, **kwargs)
    _pool.open()
    logger.info("Postgres connection pool opened.")
    return _pool


def get_pool() -> Any:
    """Return the process-wide database pool, creating it if needed."""
    return init_pool()


def close_pool() -> None:
    """Close the process-wide database pool if it has been initialized."""
    global _pool

    # Tests and graceful shutdown paths can call this safely even if the pool
    # was never opened.
    if _pool is None:
        return

    _pool.close()
    _pool = None
    logger.info("Postgres connection pool closed.")


@contextmanager
def connection() -> Iterator[Any]:
    """Borrow a connection from the process-wide pool."""
    # The pool context manager returns the connection to the pool even when
    # callers raise an exception.
    with get_pool().connection() as conn:
        yield conn


@contextmanager
def transaction() -> Iterator[Any]:
    """Borrow a pooled connection and wrap work in a database transaction."""
    # Service functions use this helper so success commits and exceptions roll
    # back consistently.
    with connection() as conn:
        with conn.transaction():
            yield conn
