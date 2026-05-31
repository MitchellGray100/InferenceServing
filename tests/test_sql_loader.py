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
    assert "list_sole_owner_projects_for_user" in store.names()


def test_account_delete_project_query_targets_sole_owned_projects() -> None:
    store = load_queries()
    query = store.get("list_sole_owner_projects_for_user")

    assert "pm.role = 'owner'" in query
    assert "other_pm.role = 'owner'" in query


def test_get_unknown_query_raises_clear_key_error() -> None:
    store = load_queries()

    with pytest.raises(KeyError, match="Unknown SQL query"):
        store.get("missing_query")


def test_parse_query_file_rejects_empty_named_query(tmp_path: Path) -> None:
    query_file = tmp_path / "empty.sql"
    query_file.write_text("-- name: empty_query\n", encoding="utf-8")

    with pytest.raises(ValueError, match="empty_query"):
        parse_query_file(query_file)
