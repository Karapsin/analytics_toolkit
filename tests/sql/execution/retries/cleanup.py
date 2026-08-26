from __future__ import annotations

from tests.sql._support.retries import (
    FakeConnection,
    operation_runner_module,
    retry_module,
)


def test_operation_runner_uses_custom_connection_cleanup() -> None:
    connection = FakeConnection("custom")
    cleaned: list[dict[str, FakeConnection]] = []

    result = operation_runner_module.run_connection_operation(
        operation_name="custom cleanup",
        connection_key="gp",
        backend="gp",
        retry_cnt=1,
        timeout_increment=0,
        open_connection=lambda _key: connection,
        operation=lambda connection_ref, _attempt: connection_ref["connection"].name,
        context_factory=lambda attempt: operation_runner_module.SqlOperationContext(
            operation="custom cleanup",
            retry_attempt=attempt,
        ),
        cleanup=cleaned.append,
    )

    assert result == "custom"
    assert cleaned == [{"connection": connection}]
    assert connection.close_calls == 0


def test_run_with_fresh_connection_returns_after_final_cleanup() -> None:
    connection = FakeConnection("fresh")
    seen_refs: list[dict[str, FakeConnection]] = []

    result = retry_module.run_with_fresh_connection(
        "warehouse",
        "target",
        lambda connection_ref: seen_refs.append(connection_ref) or "ok",
        open_connection=lambda connection_key: connection,
    )

    assert result == "ok"
    assert seen_refs == [{"connection": connection}]
    assert connection.close_calls == 1
