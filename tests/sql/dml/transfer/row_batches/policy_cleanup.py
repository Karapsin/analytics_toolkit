from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit.sql.connection.errors import SqlConfigError

from tests.sql._support.row_batches import retry_module
from tests.sql._support.stage_cleanup import load_stage_module
from tests.sql._support.transfer_keyed import (
    _metadata,
    _options,
    _task,
    staged_keyed_io,
)


@pytest.mark.parametrize("acquisition_fails", [False, True])
def test_policy_stage_cleanup_retries_only_after_an_acquired_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
    acquisition_fails: bool,
) -> None:
    connection = object()
    events: list[str] = []
    error: Exception = (
        SqlConfigError("invalid cleanup connection")
        if acquisition_fails
        else RuntimeError("temporary stage drop failure")
    )
    pending_failures = [error]

    def acquire(_key: str, _ref: dict[str, Any]) -> Any:
        events.append("acquire")
        if acquisition_fails:
            raise error
        return connection

    def cleanup(*_args: Any, **_kwargs: Any) -> None:
        events.append("drop")
        if pending_failures:
            raise pending_failures.pop(0)

    def rollback(_connection: Any) -> None:
        events.append("rollback")
        message = "rollback failed too"
        raise OSError(message)

    monkeypatch.setattr(load_stage_module, "ensure_connection_ref", acquire)
    monkeypatch.setattr(load_stage_module, "cleanup_stage_table", cleanup)

    def run() -> None:
        load_stage_module.cleanup_stage_table_with_retry(
            "ch",
            "warehouse",
            {"connection": connection},
            "scratch.stage",
            retry_fn=retry_module.run_with_retry,
            retry_cnt=3,
            timeout_increment=0,
            rollback_fn=rollback,
            replace_connection_fn=lambda _key, _ref: events.append("reconnect"),
            ch_creation_policy=object(),
        )

    if acquisition_fails:
        with pytest.raises(SqlConfigError) as caught:
            run()
        assert caught.value is error
        assert events == ["acquire"]
        assert getattr(error, "analytics_toolkit_sql_retry_safe", True) is False
    else:
        run()
        assert events == ["acquire", "drop", "rollback", "reconnect", "acquire", "drop"]
    assert getattr(error, "analytics_toolkit_sql_backend", None) == "ch"


@pytest.mark.parametrize("operation", ["read", "drop"])
def test_keyed_source_acquisition_failure_stops_without_rollback_or_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    options = _options(retry_cnt=3)
    task = _task(options)
    events: list[str] = []
    error = SqlConfigError("source connection configuration is invalid")

    def acquire(_key: str, _ref: dict[str, Any]) -> Any:
        events.append("acquire")
        raise error

    monkeypatch.setattr(staged_keyed_io, "ensure_connection_ref", acquire)
    monkeypatch.setattr(
        staged_keyed_io,
        "rollback_quietly",
        lambda _connection: events.append("rollback"),
    )
    source_ref = {
        "connection": object(),
        "bounded_replace_connection": lambda _key, _ref: events.append("reconnect"),
    }

    def run() -> None:
        if operation == "read":
            staged_keyed_io.read_key_batch(options, source_ref, task, _metadata(), 1, 2)
        else:
            staged_keyed_io.drop_source_stage(options, source_ref, task)

    with pytest.raises(SqlConfigError) as caught:
        run()

    assert caught.value is error
    assert events == ["acquire"]
    assert getattr(error, "analytics_toolkit_sql_backend", None) == "gp"
    assert getattr(error, "analytics_toolkit_sql_retry_safe", True) is False
