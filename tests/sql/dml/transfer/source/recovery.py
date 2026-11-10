from __future__ import annotations

from tests.sql._support.row_batches import pd, pytest, source_module
from tests.sql._support.source_recovery import (
    RecoveringClickHouseConnection,
    RecoveringClickHouseStream,
    RecoveringCursor,
    RecoveringDbapiConnection,
    arrange_source_recovery,
)


@pytest.mark.parametrize("failure_site", ["cursor", "execute"])
def test_dbapi_start_retries_transient_failure_on_fresh_connection(
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
) -> None:
    original_error = ConnectionError("source connection reset")
    first_cursor = RecoveringCursor(
        [], execute_error=original_error if failure_site == "execute" else None
    )
    first = RecoveringDbapiConnection(
        first_cursor, cursor_error=original_error if failure_site == "cursor" else None
    )
    second = RecoveringDbapiConnection(RecoveringCursor([(1,), (2,)]))
    opened, sleeps = arrange_source_recovery(monkeypatch, second)
    connection_ref = {"connection": first}

    batches = list(
        source_module.iter_source_batches(
            "gp_source",
            "gp",
            connection_ref,
            "SELECT id FROM source",
            batch_size=1,
            retry_cnt=2,
            timeout_increment=5,
        )
    )

    assert [batch.rows for batch in batches] == [[(1,)], [(2,)]]
    assert opened == ["gp_source"]
    assert sleeps == [5]
    assert connection_ref["connection"] is second
    assert first.cursor_calls == 1
    assert first.rollback_calls == 1
    assert first.close_calls == 1
    assert first_cursor.close_calls == (1 if failure_site == "execute" else 0)
    assert second.cursor_obj.close_calls == 1
    assert getattr(original_error, "analytics_toolkit_sql_backend", None) == "gp"


def test_dbapi_exhausted_start_preserves_original_without_unused_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_error = ConnectionError("source connection reset")
    connection = RecoveringDbapiConnection(RecoveringCursor([], execute_error=original_error))
    opened, sleeps = arrange_source_recovery(
        monkeypatch, RecoveringDbapiConnection(RecoveringCursor([(9,)]))
    )

    with pytest.raises(ConnectionError) as caught:
        list(
            source_module.iter_source_batches(
                "gp_source",
                "gp",
                {"connection": connection},
                "SELECT id FROM source",
                batch_size=1,
                retry_cnt=1,
                timeout_increment=5,
            )
        )

    assert caught.value is original_error
    assert opened == []
    assert sleeps == []
    assert connection.cursor_obj.close_calls == 1
    assert connection.close_calls == 0


def test_dbapi_fetch_interrupt_closes_cursor_without_replaying_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt()
    connection = RecoveringDbapiConnection(RecoveringCursor([(1,)], fetch_error=interrupt))
    opened, sleeps = arrange_source_recovery(monkeypatch, object())
    batches = source_module.iter_source_batches(
        "gp_source",
        "gp",
        {"connection": connection},
        "SELECT id FROM source",
        batch_size=1,
        retry_cnt=3,
        timeout_increment=5,
    )

    assert next(batches).rows == [(1,)]
    with pytest.raises(KeyboardInterrupt) as caught:
        next(batches)

    assert caught.value is interrupt
    assert connection.cursor_calls == 1
    assert connection.cursor_obj.close_calls == 1
    assert opened == []
    assert sleeps == []
    assert getattr(interrupt, "analytics_toolkit_sql_backend", None) == "gp"


@pytest.mark.parametrize("failure_site", ["query", "enter"])
def test_clickhouse_start_retries_on_fresh_connection_and_restores_limits(
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
) -> None:
    original_error = ConnectionError("source connection reset")
    first_stream = RecoveringClickHouseStream(
        [], enter_error=original_error if failure_site == "enter" else None
    )
    first = RecoveringClickHouseConnection(
        first_stream, query_error=original_error if failure_site == "query" else None
    )
    second = RecoveringClickHouseConnection(
        RecoveringClickHouseStream([pd.DataFrame({"id": [1, 2]})])
    )
    opened, sleeps = arrange_source_recovery(monkeypatch, second)
    connection_ref = {"connection": first}

    batches = list(
        source_module.iter_source_batches(
            "ch_source",
            "ch",
            connection_ref,
            "SELECT id FROM source",
            batch_size=1,
            retry_cnt=2,
            timeout_increment=5,
            disable_ch_query_limit=True,
        )
    )

    assert [batch.rows for batch in batches] == [[(1,)], [(2,)]]
    assert opened == ["ch_source"]
    assert sleeps == [5]
    assert connection_ref["connection"] is second
    assert first.close_calls == 1
    assert first_stream.exit_calls == (1 if failure_site == "enter" else 0)
    assert second.stream.exit_calls == 1
    assert first.query_limits == second.query_limits == [0]
    assert first.query_limit == second.query_limit == 10


def test_clickhouse_exhausted_start_has_no_unused_stream_or_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_error = ConnectionError("source connection reset")
    stream = RecoveringClickHouseStream([])
    connection = RecoveringClickHouseConnection(stream, query_error=original_error)
    opened, sleeps = arrange_source_recovery(monkeypatch, object())

    with pytest.raises(ConnectionError) as caught:
        list(
            source_module.iter_source_batches(
                "ch_source",
                "ch",
                {"connection": connection},
                "SELECT id FROM source",
                batch_size=1,
                retry_cnt=1,
                timeout_increment=5,
            )
        )

    assert caught.value is original_error
    assert stream.enter_calls == 0
    assert stream.exit_calls == 0
    assert connection.close_calls == 0
    assert opened == []
    assert sleeps == []


def test_clickhouse_midstream_interrupt_closes_without_wrapping_or_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt()
    stream = RecoveringClickHouseStream([pd.DataFrame({"id": [1]})], read_error=interrupt)
    connection = RecoveringClickHouseConnection(stream)
    opened, sleeps = arrange_source_recovery(monkeypatch, object())
    batches = source_module.iter_source_batches(
        "ch_source",
        "ch",
        {"connection": connection},
        "SELECT id FROM source",
        batch_size=1,
        retry_cnt=3,
        timeout_increment=5,
    )

    assert next(batches).rows == [(1,)]
    with pytest.raises(KeyboardInterrupt) as caught:
        next(batches)

    assert caught.value is interrupt
    assert stream.exit_calls == 1
    assert len(connection.queries) == 1
    assert opened == []
    assert sleeps == []
    assert getattr(interrupt, "analytics_toolkit_sql_backend", None) == "ch"
