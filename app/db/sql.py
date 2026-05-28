"""SQL file loading helpers.

Query files live in `app/db/queries`. This module will load named SQL
statements so services can keep persistence logic explicit without scattering
large SQL strings through Python code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


QUERY_MARKER_RE = re.compile(r"^--\s*name:\s*([a-zA-Z0-9_]+)\s*$")


@dataclass(frozen=True)
class Query:
    """A named SQL statement loaded from a query file."""

    name: str
    sql: str
    source: Path


class QueryStore:
    """In-memory collection of named SQL statements."""

    def __init__(self, queries: dict[str, Query]) -> None:
        self._queries = queries

    def get(self, name: str) -> str:
        """Return a SQL statement by name."""
        try:
            return self._queries[name].sql
        except KeyError as exc:
            raise KeyError(f"Unknown SQL query: {name}") from exc

    def source(self, name: str) -> Path:
        """Return the file path a named query was loaded from."""
        try:
            return self._queries[name].source
        except KeyError as exc:
            raise KeyError(f"Unknown SQL query: {name}") from exc

    def names(self) -> list[str]:
        """Return all loaded query names in sorted order."""
        return sorted(self._queries)


def queries_dir() -> Path:
    """Return the default directory containing SQL query files."""
    return Path(__file__).resolve().parent / "queries"


def parse_query_file(path: Path) -> dict[str, Query]:
    """Parse one SQL file into named queries.

    Query files use marker comments:

    ```sql
    -- name: find_user_by_email
    SELECT * FROM users WHERE email = %(email)s;
    ```
    """
    queries: dict[str, Query] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        marker = QUERY_MARKER_RE.match(line.strip())

        if marker:
            if current_name is not None:
                queries[current_name] = _build_query(
                    name=current_name,
                    lines=current_lines,
                    source=path,
                )

            current_name = marker.group(1)
            current_lines = []
            continue

        if current_name is not None:
            current_lines.append(line)

    if current_name is not None:
        queries[current_name] = _build_query(
            name=current_name,
            lines=current_lines,
            source=path,
        )

    return queries


def load_queries(directory: Path | None = None) -> QueryStore:
    """Load every named query from `.sql` files in a directory."""
    directory = directory or queries_dir()
    queries: dict[str, Query] = {}

    for path in sorted(directory.glob("*.sql")):
        for name, query in parse_query_file(path).items():
            if name in queries:
                raise ValueError(
                    f"Duplicate SQL query name `{name}` in {path} and "
                    f"{queries[name].source}"
                )
            queries[name] = query

    return QueryStore(queries)


def _build_query(name: str, lines: list[str], source: Path) -> Query:
    sql = "\n".join(lines).strip()

    if not sql:
        raise ValueError(f"SQL query `{name}` in {source} is empty")

    return Query(name=name, sql=sql, source=source)
