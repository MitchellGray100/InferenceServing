from pathlib import Path

import pytest

from app.db.sql import load_queries, parse_query_file


def test_load_queries_finds_expected_query_names() -> None:
    store = load_queries()

    assert "create_user" in store.names()
    assert "claim_next_deployment_job" in store.names()
    assert "claim_next_project_cleanup_job" in store.names()
    assert "heartbeat_model_operation_lease" in store.names()
    assert "verify_model_operation_lease" in store.names()
    assert "get_model_inference_metrics" in store.names()


def test_get_unknown_query_raises_clear_key_error() -> None:
    store = load_queries()

    with pytest.raises(KeyError, match="Unknown SQL query"):
        store.get("missing_query")


def test_parse_query_file_rejects_empty_named_query(tmp_path: Path) -> None:
    query_file = tmp_path / "empty.sql"
    query_file.write_text("-- name: empty_query\n", encoding="utf-8")

    with pytest.raises(ValueError, match="empty_query"):
        parse_query_file(query_file)
