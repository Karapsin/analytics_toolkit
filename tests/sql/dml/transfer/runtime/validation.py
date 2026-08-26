from __future__ import annotations

from tests.sql._support.transfer_runtime import (
    Any,
    BoundedConnectionCloseError,
    BoundedConnectionManager,
    ThreadPoolExecutor,
    _Connection,
    _manager_with_close_failure,
    _OpenError,
    _StateRejectingError,
    pytest,
    threading,
    time,
)


def test_close_preserving_handles_missing_error_and_nonmutable_error_state() -> None:
    without_original = _manager_with_close_failure("no original pool")
    with pytest.raises(BoundedConnectionCloseError):
        without_original.close_preserving(None)

    nonmutable_original = _StateRejectingError("immutable exception state")
    nonmutable = _manager_with_close_failure("immutable error pool")
    with pytest.raises(BoundedConnectionCloseError) as exc_info:
        nonmutable.close_preserving(nonmutable_original)
    assert exc_info.value.__cause__ is nonmutable_original


def test_close_waits_for_inflight_open_and_rejects_late_connection() -> None:
    open_started = threading.Event()
    release_open = threading.Event()
    connection = _Connection()

    def open_connection(_key: str) -> _Connection:
        open_started.set()
        assert release_open.wait(2)
        return connection

    manager = BoundedConnectionManager(
        "source",
        1,
        role="inflight close pool",
        open_connection=open_connection,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        lease_future = executor.submit(manager.run, "read", lambda _ref: None)
        assert open_started.wait(2)
        close_future = executor.submit(manager.close)
        time.sleep(0.05)
        assert not close_future.done()
        release_open.set()
        with pytest.raises(RuntimeError, match="opening was cancelled") as exc_info:
            lease_future.result(timeout=2)
        assert exc_info.value.analytics_toolkit_sql_retry_safe is False
        assert close_future.result(timeout=2) is None

    assert connection.close_calls == 1


@pytest.mark.parametrize("capacity", [True, False, 0, -1, 1.5, "2"])
def test_connection_pool_rejects_nonpositive_or_non_integer_capacity(capacity: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        BoundedConnectionManager("source", capacity, role="validation pool")


def test_replace_failure_leaves_ref_reopenable_and_validates_ownership() -> None:
    attempts = 0
    opened: list[_Connection] = []

    def open_connection(_key: str) -> _Connection:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise _OpenError
        connection = _Connection()
        opened.append(connection)
        return connection

    manager = BoundedConnectionManager(
        "source",
        1,
        role="replacement pool",
        open_connection=open_connection,
    )
    with manager.lease() as ref:
        with pytest.raises(RuntimeError, match="does not belong"):
            manager.replace_connection("other", ref)
        with pytest.raises(RuntimeError, match="does not belong"):
            manager.replace_connection("source", {})
        with pytest.raises(RuntimeError, match="does not belong"):
            manager.ensure_connection("other", ref)
        with pytest.raises(RuntimeError, match="does not belong"):
            manager.ensure_connection("source", {})
        with pytest.raises(RuntimeError, match="Could not replace"):
            manager.replace_connection("source", ref)
        assert "connection" not in ref
        manager.ensure_connection("source", ref)
        manager.ensure_connection("source", ref)

    assert attempts == 3
    assert opened[0].close_calls == 1
    manager.close()
    assert opened[1].close_calls == 1
