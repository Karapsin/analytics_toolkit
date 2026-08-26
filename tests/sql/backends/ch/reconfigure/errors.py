from __future__ import annotations

from tests.sql._support.reconfigure import (
    CountingReconfigureClient,
    FakeClickHouseResult,
    ReconfigureClient,
    SimpleNamespace,
    _options,
    get_backend_adapter,
    plan_ch_table_reconfiguration,
    pytest,
    reconfigure_api,
    reconfigure_backend,
    sql,
)


def test_cleanup_failure_is_returned_as_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CountingReconfigureClient([3, 3, 3, 3])
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="MergeTree"),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)
    monkeypatch.setattr(
        reconfigure_backend,
        "_wait_for_cleanup",
        lambda *_: (_ for _ in ()).throw(TimeoutError("still visible")),
    )

    reconfigure_backend.execute_ch_table_reconfiguration(
        adapter,
        client,
        reconfiguration,
        validate_row_count=False,
    )

    assert reconfiguration.cleanup_complete is False
    assert reconfiguration.cleanup_error == "TimeoutError: still visible"


def test_failed_rollback_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    client = CountingReconfigureClient([3, 3, 3])
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="MergeTree"),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)

    def execute_phase(_adapter: object, _connection: object, _plan: object, phase: str) -> None:
        if phase == "cutover":
            message = "cutover failed"
            raise RuntimeError(message)

    monkeypatch.setattr(reconfigure_backend, "_execute_phase", execute_phase)
    monkeypatch.setattr(
        reconfigure_backend,
        "_execute_sqls",
        lambda *_: (_ for _ in ()).throw(RuntimeError("rollback failed")),
    )

    with pytest.raises(RuntimeError, match="rollback also failed"):
        reconfigure_backend.execute_ch_table_reconfiguration(
            adapter,
            client,
            reconfiguration,
            validate_row_count=True,
        )


def test_public_failure_builds_operation_context(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingClient(ReconfigureClient):
        def query(self, query: str) -> FakeClickHouseResult:
            message = f"failed query: {query}"
            raise RuntimeError(message)

    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _db_key: SimpleNamespace(connection_key="ch", backend="ch"),
    )
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _db_key: FailingClient())

    with pytest.raises(RuntimeError, match="failed query"):
        sql.ch_reconfigure_table(
            "ch",
            "analytics.events",
            ch_settings={"index_granularity": 4096},
            retry_cnt=1,
        )
