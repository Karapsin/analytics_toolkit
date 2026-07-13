from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

source_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.io.source"
)


class FakeClickHouseStream:
    def __init__(self, blocks: list[pd.DataFrame]) -> None:
        self.blocks = blocks
        self.exit_calls = 0

    def __enter__(self) -> Any:
        return iter(self.blocks)

    def __exit__(self, *args: Any) -> None:
        self.exit_calls += 1


class FakeClickHouseConnection:
    def __init__(self, blocks: list[pd.DataFrame]) -> None:
        self.context = FakeClickHouseStream(blocks)
        self.queries: list[str] = []
        self.query_limit: int | None = None
        self.query_limits_seen: list[int | None] = []

    def query_df_stream(self, query: str) -> FakeClickHouseStream:
        self.queries.append(query)
        self.query_limits_seen.append(self.query_limit)
        return self.context


class ProtocolError(Exception):
    pass


class FailingClickHouseStream:
    def __init__(self, blocks: list[pd.DataFrame], error: Exception) -> None:
        self.blocks = list(blocks)
        self.error = error
        self.exit_calls = 0

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *args: Any) -> None:
        self.exit_calls += 1

    def __iter__(self) -> FailingClickHouseStream:
        return self

    def __next__(self) -> pd.DataFrame:
        if self.blocks:
            return self.blocks.pop(0)
        raise self.error


class FailingClickHouseConnection:
    def __init__(self, stream: FailingClickHouseStream) -> None:
        self.context = stream
        self.queries: list[str] = []

    def query_df_stream(self, query: str) -> FailingClickHouseStream:
        self.queries.append(query)
        return self.context


class FakeDbapiCursor:
    def __init__(self, rows: list[Any], *, execute_error: Exception | None = None) -> None:
        self.rows = list(rows)
        self.execute_error = execute_error
        self.description = [("id",), ("label",)]
        self.executed: list[str] = []
        self.fetch_sizes: list[int] = []
        self.close_calls = 0

    def execute(self, query: str) -> None:
        self.executed.append(query)
        if self.execute_error is not None:
            raise self.execute_error

    def fetchmany(self, size: int) -> list[Any]:
        self.fetch_sizes.append(size)
        batch = self.rows[:size]
        self.rows = self.rows[size:]
        return batch

    def close(self) -> None:
        self.close_calls += 1


class FakeDbapiConnection:
    def __init__(self, cursor: FakeDbapiCursor) -> None:
        self.cursor_obj = cursor

    def cursor(self) -> FakeDbapiCursor:
        return self.cursor_obj


def test_clickhouse_batches_drain_pending_rows_without_spinning() -> None:
    connection = FakeClickHouseConnection(
        [
            pd.DataFrame({"id": [1, 2]}),
            pd.DataFrame({"id": [3, 4]}),
            pd.DataFrame({"id": [5]}),
        ]
    )
    batch_size_calls = 0

    def get_batch_size() -> int:
        nonlocal batch_size_calls
        batch_size_calls += 1
        if batch_size_calls > 10:
            raise AssertionError("ClickHouse batch draining is not making progress.")
        return 3

    batches = list(
        source_module.iter_source_batches(
            "ch",
            "ch",
            {"connection": connection},
            "select id from source",
            batch_size=3,
            retry_cnt=1,
            timeout_increment=0,
            get_batch_size=get_batch_size,
        )
    )

    assert [batch.rows for batch in batches] == [[(1,), (2,), (3,)], [(4,), (5,)]]
    assert connection.queries == ["select id from source"]
    assert connection.context.exit_calls == 1


def test_clickhouse_stream_temporarily_disables_client_query_limit() -> None:
    connection = FakeClickHouseConnection([pd.DataFrame({"id": [1, 2]})])
    connection.query_limit = 1_728_512

    batches = list(
        source_module.iter_source_batches(
            "ch",
            "ch",
            {"connection": connection},
            "select id from source limit 6582921",
            batch_size=10,
            retry_cnt=1,
            timeout_increment=0,
            disable_ch_query_limit=True,
        )
    )

    assert [batch.rows for batch in batches] == [[(1,), (2,)]]
    assert connection.query_limits_seen == [0]
    assert connection.query_limit == 1_728_512


def test_clickhouse_midstream_transport_error_is_wrapped() -> None:
    connection = FailingClickHouseConnection(
        FailingClickHouseStream(
            [pd.DataFrame({"id": [1]})],
            ProtocolError("unexpected failure to read next chunk"),
        )
    )
    batches = source_module.iter_source_batches(
        "ch_source",
        "ch",
        {"connection": connection},
        "select id from source",
        batch_size=1,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert next(batches).rows == [(1,)]
    with pytest.raises(
        source_module.TransferSourceStreamReadError,
        match="ClickHouse source stream read failed",
    ):
        next(batches)

    assert connection.context.exit_calls == 1


def test_dbapi_batches_use_dynamic_sizes_convert_rows_and_close() -> None:
    cursor = FakeDbapiCursor([[1, "a"], [2, "b"], [3, "c"]])
    sizes = iter([2, 1, 1])

    batches = list(
        source_module._iter_dbapi_batches(
            "gp_source",
            "gp",
            {"connection": FakeDbapiConnection(cursor)},
            "select id, label from source",
            get_batch_size=lambda: next(sizes),
            retry_cnt=1,
            timeout_increment=0,
        )
    )

    assert [batch.rows for batch in batches] == [[(1, "a"), (2, "b")], [(3, "c")]]
    assert cursor.fetch_sizes == [2, 1, 1]
    assert cursor.close_calls == 1


def test_dbapi_batch_read_error_logs_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = FakeDbapiCursor([])
    messages: list[str] = []
    cursor.fetchmany = lambda _size: (_ for _ in ()).throw(RuntimeError("read failed"))
    monkeypatch.setattr(
        source_module,
        "time_print",
        lambda message, **_kwargs: messages.append(message),
    )

    with pytest.raises(RuntimeError, match="read failed"):
        list(
            source_module._iter_dbapi_batches(
                "gp_source",
                "gp",
                {"connection": FakeDbapiConnection(cursor)},
                "select id from source",
                get_batch_size=lambda: 10,
                retry_cnt=1,
                timeout_increment=0,
            )
        )
    assert messages == ["Failed SQL while reading transfer source:\nselect id from source"]
    assert cursor.close_calls == 1


def test_dbapi_query_start_failure_closes_rolls_back_and_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = FakeDbapiCursor([], execute_error=RuntimeError("execute failed"))
    connection_ref = {"connection": FakeDbapiConnection(cursor)}
    rollbacks: list[Any] = []
    replacements: list[str] = []
    monkeypatch.setattr(
        source_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(
            rollback_quietly=rollbacks.append
        ),
    )
    monkeypatch.setattr(
        source_module,
        "replace_connection",
        lambda key, _ref: replacements.append(key),
    )

    with pytest.raises(RuntimeError, match="execute failed"):
        source_module._start_dbapi_query_with_retry(
            "gp_source",
            "gp",
            connection_ref,
            "select id from source",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert cursor.close_calls == 1
    assert rollbacks == [connection_ref["connection"]]
    assert replacements == ["gp_source"]


def test_clickhouse_stream_skips_empty_blocks_and_handles_empty_stream() -> None:
    connection = FakeClickHouseConnection(
        [pd.DataFrame(), pd.DataFrame({"id": [1]}), pd.DataFrame()]
    )
    batches = list(
        source_module._iter_clickhouse_batches(
            "ch_source",
            {"connection": connection},
            "select id from source",
            get_batch_size=lambda: 10,
            retry_cnt=1,
            timeout_increment=0,
            disable_query_limit=False,
        )
    )
    assert [batch.rows for batch in batches] == [[(1,)]]
    assert connection.context.exit_calls == 1

    empty_connection = FakeClickHouseConnection([pd.DataFrame(), pd.DataFrame()])
    assert list(
        source_module._iter_clickhouse_batches(
            "ch_source",
            {"connection": empty_connection},
            "select id from empty_source",
            get_batch_size=lambda: 10,
            retry_cnt=1,
            timeout_increment=0,
            disable_query_limit=False,
        )
    ) == []
    assert empty_connection.context.exit_calls == 1


def test_clickhouse_stream_start_failure_closes_and_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FailingClickHouseStream([], RuntimeError("unused"))
    stream.__enter__ = lambda: (_ for _ in ()).throw(RuntimeError("enter failed"))
    connection = FailingClickHouseConnection(stream)
    replacements: list[str] = []
    monkeypatch.setattr(
        source_module,
        "replace_connection",
        lambda key, _ref: replacements.append(key),
    )

    with pytest.raises(RuntimeError, match="enter failed"):
        source_module._start_clickhouse_stream_with_retry(
            "ch_source",
            {"connection": connection},
            "select id from source",
            retry_cnt=1,
            timeout_increment=0,
            disable_query_limit=False,
        )
    assert stream.exit_calls == 1
    assert replacements == ["ch_source"]


def test_stream_error_context_and_transport_detection() -> None:
    error = source_module.TransferSourceStreamReadError(
        connection_key="ch_source",
        backend="ch",
        query="select id from source",
        original_exception=RuntimeError("broken"),
    )
    assert error.with_retry_context(
        target_table="sandbox.target",
        retry_batch_size=50,
        full_retry_attempt=2,
    ) is error
    assert "target_table=sandbox.target" in str(error)
    assert "retry_batch_size=50" in str(error)

    outer = RuntimeError("wrapper", RuntimeError("response ended prematurely"))
    assert source_module._is_clickhouse_stream_transport_error(outer) is True
    assert source_module._is_clickhouse_stream_transport_error(RuntimeError("ordinary")) is False
    outer.__cause__ = outer
    assert list(source_module._exception_chain(outer)) == [outer, outer.args[1]]


def test_clickhouse_columns_can_be_discovered_after_stream_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeClickHouseStream([])
    iterator = iter([pd.DataFrame(), pd.DataFrame({"id": [1, 2]})])
    monkeypatch.setattr(
        source_module,
        "_start_clickhouse_stream_with_retry",
        lambda *_args, **_kwargs: (context, iterator, None),
    )
    batches = list(
        source_module._iter_clickhouse_batches(
            "ch_source",
            {"connection": object()},
            "select id from source",
            get_batch_size=lambda: 10,
            retry_cnt=1,
            timeout_increment=0,
            disable_query_limit=False,
        )
    )
    assert [batch.rows for batch in batches] == [[(1,), (2,)]]
    assert context.exit_calls == 1


def test_clickhouse_non_transport_error_is_preserved_and_start_error_has_no_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FailingClickHouseConnection(
        FailingClickHouseStream(
            [pd.DataFrame({"id": [1]})],
            ValueError("bad dataframe"),
        )
    )
    with pytest.raises(ValueError, match="bad dataframe"):
        list(
            source_module._iter_clickhouse_batches(
                "ch_source",
                {"connection": connection},
                "select id from source",
                get_batch_size=lambda: 1,
                retry_cnt=1,
                timeout_increment=0,
                disable_query_limit=False,
            )
        )

    monkeypatch.setattr(
        source_module,
        "_start_clickhouse_stream_with_retry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("start failed")),
    )
    with pytest.raises(ValueError, match="start failed"):
        list(
            source_module._iter_clickhouse_batches(
                "ch_source",
                {"connection": object()},
                "select id from source",
                get_batch_size=lambda: 10,
                retry_cnt=1,
                timeout_increment=0,
                disable_query_limit=False,
            )
        )


def test_exception_chain_traverses_context() -> None:
    inner = RuntimeError("inner")
    outer = RuntimeError("outer")
    outer.__context__ = inner
    assert list(source_module._exception_chain(outer)) == [outer, inner]


def test_clickhouse_first_block_can_be_returned_without_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeClickHouseStream([])
    monkeypatch.setattr(
        source_module,
        "_start_clickhouse_stream_with_retry",
        lambda *_args, **_kwargs: (
            context,
            None,
            pd.DataFrame({"id": [1]}),
        ),
    )
    batches = list(
        source_module._iter_clickhouse_batches(
            "ch_source",
            {"connection": object()},
            "select id from source",
            get_batch_size=lambda: 10,
            retry_cnt=1,
            timeout_increment=0,
            disable_query_limit=False,
        )
    )
    assert [batch.rows for batch in batches] == [[(1,)]]
    assert context.exit_calls == 1
