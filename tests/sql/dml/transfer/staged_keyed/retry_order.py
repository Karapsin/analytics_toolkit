from __future__ import annotations

from analytics_toolkit.sql.connection.errors import SqlConfigError
from analytics_toolkit.sql.execution.cancellation import (
    AsyncSqlCancelled,
    SqlCancellationScope,
    activate_cancellation_scope,
)

from tests.sql._support.keyed_retry_order import arrange_keyed_retry
from tests.sql._support.staged_keyed import (
    _metadata,
    _options,
    _ready_task,
    pytest,
    staged_keyed_io,
)


def test_source_read_replaces_connection_after_retry_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(retry_cnt=2, timeout_increment=5)
    task = _ready_task((options.transfer_slices or [])[0], "source.stage", 1)
    probe, manager = arrange_keyed_retry(monkeypatch)

    with manager.lease() as source_ref:
        batch = staged_keyed_io.read_key_batch(options, source_ref, task, _metadata(), 1, 2)

    assert batch.rows == [(1,)]
    assert probe.events == ["failed_read", "rollback", "wait", "replacement", "query", "rollback"]
    assert probe.open_calls == 2
    assert manager.high_water_mark == 1
    manager.close()
    assert [connection.close_calls for connection in probe.connections] == [1, 1]


def test_source_read_cancellation_during_retry_wait_opens_no_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(retry_cnt=3, timeout_increment=5)
    task = _ready_task((options.transfer_slices or [])[0], "source.stage", 1)
    probe, manager = arrange_keyed_retry(monkeypatch)
    scope = SqlCancellationScope()

    def cancel_during_wait(_seconds: float) -> None:
        probe.events.append("wait")
        scope.request_cancel()

    monkeypatch.setattr(scope, "wait", cancel_during_wait)
    cancelled_scope = activate_cancellation_scope(scope)
    with manager.lease() as source_ref, cancelled_scope, pytest.raises(AsyncSqlCancelled):
        staged_keyed_io.read_key_batch(options, source_ref, task, _metadata(), 1, 2)

    assert probe.events == ["failed_read", "rollback", "wait"]
    assert probe.open_calls == probe.read_calls == 1
    manager.close()
    assert probe.connections[0].close_calls == 1


def test_source_read_does_not_reopen_after_terminal_replacement_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(retry_cnt=3, timeout_increment=5)
    task = _ready_task((options.transfer_slices or [])[0], "source.stage", 1)
    original_error = SqlConfigError("replacement credentials are invalid")
    probe, manager = arrange_keyed_retry(monkeypatch, replacement_error=original_error)

    with manager.lease() as source_ref, pytest.raises(RuntimeError) as caught:
        staged_keyed_io.read_key_batch(options, source_ref, task, _metadata(), 1, 2)

    assert caught.value.__cause__ is original_error
    assert probe.events == ["failed_read", "rollback", "wait", "replacement"]
    assert probe.open_calls == 2
    assert probe.read_calls == 1
    manager.close()


def test_source_read_does_not_add_open_retry_for_unmanaged_empty_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(retry_cnt=2, timeout_increment=5)
    task = _ready_task((options.transfer_slices or [])[0], "source.stage", 1)
    original_error = ConnectionError("replacement open failed")
    probe, manager = arrange_keyed_retry(monkeypatch, replacement_error=original_error)

    with manager.lease() as source_ref:
        source_ref.pop("bounded_ensure_connection")
        with pytest.raises(RuntimeError) as caught:
            staged_keyed_io.read_key_batch(options, source_ref, task, _metadata(), 1, 2)

    assert caught.value.__cause__ is original_error
    assert probe.events == ["failed_read", "rollback", "wait", "replacement"]
    assert probe.open_calls == 2
    assert probe.read_calls == 1
    manager.close()
