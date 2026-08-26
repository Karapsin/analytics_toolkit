from __future__ import annotations

from tests.sql._support.transfer_schema import (
    get_backend_adapter,
    pd,
    pytest,
    retry_module,
    stage_module,
)


def test_clickhouse_upsert_finalization_error_is_marked_unsafe() -> None:
    error = RuntimeError("ambiguous finalization")

    get_backend_adapter("ch").mark_upsert_finalization_error(error)

    assert error.__dict__["analytics_toolkit_sql_retry_safe"] is False


def test_create_stage_table_retries_collision_and_uses_explicit_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_names = iter(["sales.target__stage__first", "sales.target__stage__second"])
    existence_checks: list[tuple[str, str]] = []
    created: dict[str, object] = {}
    messages: list[str] = []
    connection = object()

    monkeypatch.setattr(
        stage_module,
        "build_stage_table_name",
        lambda *args, **kwargs: next(stage_names),
    )

    def fake_table_exists(
        connection_type: str,
        connection: object,
        table_name: str,
        *,
        connection_key: str,
    ) -> bool:
        del connection
        existence_checks.append((table_name, connection_key))
        return table_name.endswith("first")

    def fake_create(*args: object, **kwargs: object) -> None:
        created["args"] = args
        created["kwargs"] = kwargs

    monkeypatch.setattr(stage_module, "table_exists", fake_table_exists)
    monkeypatch.setattr(stage_module, "_create_sql_table_with_connection", fake_create)
    monkeypatch.setattr(stage_module, "time_print", messages.append)

    result = stage_module.create_stage_table(
        "gp",
        connection,
        "sales.target",
        pd.DataFrame({"id": [1]}),
        column_types={"id": "INTEGER"},
        table_schema={"id": "BIGINT"},
        gp_distributed_by_key=["id"],
        connection_key="warehouse",
        query_label="load stage",
    )

    assert result == "sales.target__stage__second"
    assert existence_checks == [
        ("sales.target__stage__first", "warehouse"),
        ("sales.target__stage__second", "warehouse"),
    ]
    assert messages == [
        "Stage table name collision detected for sales.target__stage__first; "
        "retrying with a new name (1/10)"
    ]
    assert created["args"][0:4] == (
        "gp",
        connection,
        "sales.target__stage__second",
        None,
    )
    assert created["kwargs"] == {
        "connection_key": "warehouse",
        "ddl_scope": "staging",
        "gp_distributed_by_key": ["id"],
        "query_label": "load stage",
        "table_schema": {"id": "BIGINT"},
    }


def test_retry_marker_prevents_unsafe_operation_retry() -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        error = RuntimeError("ambiguous finalization")
        error.analytics_toolkit_sql_retry_safe = False
        raise error

    with pytest.raises(RuntimeError, match="ambiguous finalization"):
        retry_module.run_with_retry("unsafe write", 3, 0, operation)

    assert attempts == [1]


def test_stage_cleanup_helpers_forward_retry_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    retry_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        stage_module,
        "drop_table",
        lambda *args, **kwargs: direct_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        stage_module,
        "drop_table_with_retry",
        lambda *args, **kwargs: retry_calls.append((args, kwargs)),
    )

    stage_module.cleanup_stage_table(
        "ch",
        "connection",
        "analytics.stage",
        query_label="cleanup",
        if_exists=False,
    )
    retry_fn = object()
    rollback_fn = object()
    replace_connection_fn = object()
    connection_ref = {"connection": "old"}
    stage_module.cleanup_stage_table_with_retry(
        "gp",
        "warehouse",
        connection_ref,
        "sales.stage",
        retry_fn=retry_fn,
        retry_cnt=3,
        timeout_increment=0.5,
        rollback_fn=rollback_fn,
        replace_connection_fn=replace_connection_fn,
        query_label="cleanup retry",
        if_exists=False,
    )

    assert direct_calls == [
        (
            ("ch", "connection", "analytics.stage"),
            {"query_label": "cleanup", "if_exists": False},
        )
    ]
    assert retry_calls == [
        (
            ("gp", "warehouse", connection_ref, "sales.stage"),
            {
                "retry_fn": retry_fn,
                "retry_cnt": 3,
                "timeout_increment": 0.5,
                "rollback_fn": rollback_fn,
                "replace_connection_fn": replace_connection_fn,
                "query_label": "cleanup retry",
                "if_exists": False,
            },
        )
    ]
