from __future__ import annotations

from tests.sql._support.lifecycle import (
    Any,
    InsertCursor,
    InsertDbApiAdapter,
    LifecycleAdapter,
    SimpleNamespace,
    SqlOperationContext,
    SqlOperationError,
    errors,
    maintenance,
    pytest,
)


def test_dbapi_insert_without_transactions_closes_on_success_and_failure() -> None:
    adapter = InsertDbApiAdapter(backend="test", commit_commands=False)
    success_cursor = InsertCursor()
    success_connection = SimpleNamespace(cursor=lambda: success_cursor)
    assert (
        adapter.insert_from_query(
            success_connection, "target", "SELECT 1", {}, query_label="contract"
        )
        == 4
    )
    assert success_cursor.closed is True

    failed_cursor = InsertCursor(RuntimeError("insert failed"))
    failed_connection = SimpleNamespace(cursor=lambda: failed_cursor)
    with pytest.raises(RuntimeError, match="insert failed"):
        adapter.insert_from_query(failed_connection, "target", "SELECT 1", {})
    assert failed_cursor.closed is True


@pytest.mark.parametrize(
    ("connection_key", "expected_retry"),
    [(None, False), ("ch_alias", True)],
)
def test_drop_ch_distributed_table_pair_forwards_wait_and_retry_options(
    lifecycle_adapter: LifecycleAdapter,
    connection_key: str | None,
    expected_retry: bool,
) -> None:
    maintenance.drop_ch_distributed_table_pair(
        "connection",
        "db.target",
        connection_key=connection_key,
        wait_for_absence=True,
        wait_timeout_seconds=7,
        wait_poll_interval_seconds=0.25,
    )

    call = lifecycle_adapter.calls[0]
    assert call[0] == "drop_table_with_options"
    assert call[2]["connection_key"] == (connection_key or "")
    assert call[2]["ch_retry_per_host_drops"] is expected_retry
    assert call[2]["ch_wait_timeout_seconds"] == 7


def test_drop_table_with_retry_rolls_back_replaces_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
    lifecycle_adapter: LifecycleAdapter,
) -> None:
    connection_ref = {"connection": object()}
    replacements: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        maintenance,
        "drop_table",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("drop failed")),
    )

    def retry_fn(**kwargs: Any) -> None:
        kwargs["operation"](1)

    with pytest.raises(RuntimeError, match="drop failed"):
        maintenance.drop_table_with_retry(
            "gp",
            "alias",
            connection_ref,
            "schema.stage",
            retry_fn,
            retry_cnt=0,
            timeout_increment=0,
            rollback_fn=None,
            replace_connection_fn=lambda key, ref: replacements.append((key, ref)),
        )

    assert lifecycle_adapter.calls[0] == (
        "rollback_quietly",
        (connection_ref["connection"],),
        {},
    )
    assert replacements == [("alias", connection_ref)]


def test_drop_table_with_retry_runs_successful_operation(
    monkeypatch: pytest.MonkeyPatch,
    lifecycle_adapter: LifecycleAdapter,
) -> None:
    retry_kwargs: dict[str, Any] = {}
    drops: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        maintenance,
        "drop_table",
        lambda *args, **kwargs: drops.append((*args, kwargs)),
    )

    def retry_fn(**kwargs: Any) -> None:
        retry_kwargs.update(kwargs)
        kwargs["operation"](1)

    maintenance.drop_table_with_retry(
        "gp",
        "alias",
        {"connection": "connection"},
        "schema.stage",
        retry_fn,
        retry_cnt=2,
        timeout_increment=0.5,
        rollback_fn=None,
        replace_connection_fn=lambda *_args: None,
        query_label="q",
        if_exists=False,
    )

    assert drops[0][:3] == ("gp", "connection", "schema.stage")
    assert retry_kwargs["retry_cnt"] == 2
    assert retry_kwargs["timeout_increment"] == 0.5
    assert "dropping stage table" in retry_kwargs["operation_name"]
    assert lifecycle_adapter.calls == []


def test_sql_exception_context_annotation_and_operation_error() -> None:
    context = SqlOperationContext(
        operation="transfer",
        alias="target",
        backend="gp",
        phase="insert",
        target_table="schema.target",
        source_table="schema.source",
        retry_attempt=2,
        sql_preview="INSERT ...",
    )
    original = RuntimeError("failed")

    assert errors.annotate_sql_exception(original, context) is original
    assert original.sql_context is context
    assert "target_table=schema.target" in original.__notes__[0]

    wrapped = errors.operation_error(original, context)
    assert isinstance(wrapped, SqlOperationError)
    assert wrapped.context is context
    assert "alias=target" in str(wrapped)
    assert "backend=gp" in str(wrapped)
    assert "phase=insert" in str(wrapped)
