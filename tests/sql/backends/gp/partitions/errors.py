from __future__ import annotations

from tests.sql._support.partitions import (
    Any,
    Event,
    FakeDbapiConnection,
    Lock,
    _FailingOnceGpConnection,
    _stub_leaf_partition_discovery,
    gp_maintenance_module,
    pytest,
    sleep,
    table_ops_module,
)


def test_gp_analyze_partitioned_table_stops_scheduling_after_concurrent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_leaf_partition_discovery(
        monkeypatch,
        [
            "analytics.orders_1_prt_a",
            "analytics.orders_1_prt_b",
            "analytics.orders_1_prt_c",
        ],
    )
    started = Event()
    lock = Lock()
    opened = 0

    class FailingConnection(FakeDbapiConnection):
        def cursor(self) -> Any:
            cursor = super().cursor()
            original_execute = cursor.execute

            def execute(query: str, *args: Any, **kwargs: Any) -> Any:
                started.set()
                raise RuntimeError("analyze failed")  # noqa: EM101, TRY003

            cursor.execute = execute
            del original_execute
            return cursor

    class WaitingConnection(FakeDbapiConnection):
        def cursor(self) -> Any:
            cursor = super().cursor()
            original_execute = cursor.execute

            def execute(query: str, *args: Any, **kwargs: Any) -> Any:
                assert started.wait(timeout=1)
                sleep(0.02)
                return original_execute(query, *args, **kwargs)

            cursor.execute = execute
            return cursor

    def open_connection(_key: str) -> FakeDbapiConnection:
        nonlocal opened
        with lock:
            opened += 1
            return FailingConnection() if opened == 1 else WaitingConnection()

    monkeypatch.setattr(gp_maintenance_module, "get_sql_connection", open_connection)

    with pytest.raises(RuntimeError, match="analyze failed"):
        gp_maintenance_module.gp_analyze_partitioned_table(
            "gp",
            "analytics.orders",
            [
                "analytics.orders_1_prt_a",
                "analytics.orders_1_prt_b",
                "analytics.orders_1_prt_c",
            ],
            concurrency=2,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert opened == 2


def test_gp_create_partitions_retries_with_fresh_connection_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_connection = _FailingOnceGpConnection()
    second_connection = _FailingOnceGpConnection(fail_first_execute=False)
    connections = [first_connection, second_connection]

    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: connections.pop(0),
    )

    result = table_ops_module.gp_create_partitions(
        "gp",
        "sandbox.events",
        days=["2026-05-01", "2026-05-02"],
        retry_cnt=2,
        timeout_increment=0,
        return_metadata=True,
    )

    assert first_connection.executed == [
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_05_01 "
        "START ('2026-05-01') INCLUSIVE END ('2026-05-02') EXCLUSIVE"
    ]
    assert first_connection.rollback_calls >= 1
    assert first_connection.commit_calls == 0
    assert first_connection.close_calls == 1
    assert second_connection.executed == [
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_05_01 "
        "START ('2026-05-01') INCLUSIVE END ('2026-05-02') EXCLUSIVE",
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_05_02 "
        "START ('2026-05-02') INCLUSIVE END ('2026-05-03') EXCLUSIVE",
    ]
    assert second_connection.rollback_calls == 0
    assert second_connection.commit_calls == 1
    assert second_connection.close_calls == 1
    assert result.metadata.retry_attempts == 2
    assert result.metadata.operation_status == "success"
