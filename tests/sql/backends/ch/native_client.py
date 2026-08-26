from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.native_client import (
    NativeClickHouseClient,
    NativeQueryResult,
    _as_insert_bytes,
    _is_column_metadata,
    _is_string_type,
    _NativeDataFrameStream,
    _normalize_stream_rows,
    _split_first_stream_block,
)


class FakeNativeClient:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.dataframe = pd.DataFrame({"value": [1, 2]})
        self.stream_blocks: list[Any] = [
            ([(1, "a")], [("value", "Int64"), ("label", "Nullable(String)")]),
            [(2, "b"), (3, "c")],
        ]
        self.disconnects = 0
        self.insert_dataframe_calls: list[tuple[str, pd.DataFrame, dict[str, Any]]] = []
        self.last_query: Any = None

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self.execute_calls.append((sql, args, kwargs))
        if kwargs.get("with_column_types"):
            if kwargs.get("columnar"):
                return ([[1, 2], ["a", "b"]], [("value", "Int64"), ("label", "String")])
            return ([(1, "a"), (2, "b")], [("value", "Int64"), ("label", "String")])
        return []

    def query_dataframe(self, sql: str) -> pd.DataFrame:
        assert sql == "select dataframe"
        return self.dataframe

    def execute_iter(self, sql: str, **kwargs: Any) -> Any:
        assert sql == "select stream"
        assert kwargs == {"with_column_types": True, "chunk_size": 65_536}
        return iter(self.stream_blocks)

    def insert_dataframe(self, sql: str, df: pd.DataFrame, **kwargs: Any) -> int:
        self.insert_dataframe_calls.append((sql, df, kwargs))
        return len(df)

    def disconnect_connection(self) -> None:
        self.disconnects += 1


def test_command_and_query_compatibility() -> None:
    raw = FakeNativeClient()
    client = NativeClickHouseClient(raw)

    assert client.command("truncate table x") == []
    assert client.command("drop table x", settings={"timeout": 0}) == []
    rows = client.query("select rows")
    columns = client.query("select columns", column_oriented=True)
    configured = client.query("select configured", settings={"max_threads": 2})

    assert rows.column_names == ("value", "label")
    assert rows.result_rows == [(1, "a"), (2, "b")]
    assert rows.result_columns == [(1, 2), ("a", "b")]
    assert rows.row_count == 2
    assert columns.result_rows == [(1, "a"), (2, "b")]
    assert columns.result_columns == [(1, 2), ("a", "b")]
    assert columns.row_count == 2
    assert configured.column_types == ("Int64", "String")
    assert raw.execute_calls[1][2] == {"settings": {"timeout": 0}}
    assert raw.execute_calls[-1][2]["settings"] == {"max_threads": 2}


def test_command_exposes_native_insert_select_written_rows() -> None:
    raw = FakeNativeClient()
    raw.last_query = type(
        "LastQuery",
        (),
        {"progress": type("Progress", (), {"written_rows": 7})()},
    )()

    result = NativeClickHouseClient(raw).command("insert into target select * from source")

    assert result == {"written_rows": 7}


def test_empty_query_result_preserves_metadata() -> None:
    rows = NativeQueryResult([], [("value", "Nullable(Int64)")], column_oriented=False)
    columns = NativeQueryResult([], [("value", "Array(String)")], column_oriented=True)

    assert rows.result_rows == []
    assert rows.result_rows == []
    assert rows.result_columns == [()]
    assert rows.result_columns == [()]
    assert rows.row_count == 0
    assert columns.result_rows == []
    assert columns.result_columns == []
    assert columns.row_count == 0


def test_dataframe_read_and_streaming() -> None:
    raw = FakeNativeClient()
    client = NativeClickHouseClient(raw)

    assert client.query_df("select dataframe") is raw.dataframe
    with client.query_df_stream("select stream") as stream:
        blocks = list(stream)

    assert [block.to_dict("records") for block in blocks] == [
        [{"value": 1, "label": "a"}],
        [{"value": 2, "label": "b"}, {"value": 3, "label": "c"}],
    ]
    assert raw.disconnects == 0


def test_empty_stream_and_early_exit_cleanup() -> None:
    raw = FakeNativeClient()
    raw.stream_blocks = [([], [("value", "Array(Tuple(String, UInt64))")])]
    client = NativeClickHouseClient(raw)
    with client.query_df_stream("select stream") as stream:
        assert list(stream) == []
    assert raw.disconnects == 0

    raw.stream_blocks = [
        ([(1,)], [("value", "Int64")]),
        [(2,)],
    ]
    with client.query_df_stream("select stream") as stream:
        assert next(stream).iloc[0, 0] == 1
    assert raw.disconnects == 1


def test_stream_with_no_driver_items_is_empty() -> None:
    raw = FakeNativeClient()
    raw.stream_blocks = []
    client = NativeClickHouseClient(raw)

    with client.query_df_stream("select stream") as stream:
        assert list(stream) == []

    assert raw.disconnects == 0


def test_stream_ignores_empty_followup_blocks() -> None:
    raw = FakeNativeClient()
    raw.stream_blocks = [
        ([(1,)], [("value", "Int64")]),
        [],
    ]
    client = NativeClickHouseClient(raw)

    with client.query_df_stream("select stream") as stream:
        assert [block.iloc[0, 0] for block in stream] == [1]

    assert raw.disconnects == 0


def test_stream_exception_disconnects() -> None:
    raw = FakeNativeClient()

    def failing_blocks() -> Any:
        yield ([(1,)], [("value", "Int64")])
        message = "stream failed"
        raise RuntimeError(message)

    raw.stream_blocks = failing_blocks()  # type: ignore[assignment]
    client = NativeClickHouseClient(raw)
    with pytest.raises(RuntimeError, match="stream failed"):  # noqa: SIM117
        with client.query_df_stream("select stream") as stream:
            list(stream)
    assert raw.disconnects == 1


def test_insert_quoting_and_idempotent_close() -> None:
    raw = FakeNativeClient()
    client = NativeClickHouseClient(raw)
    frame = pd.DataFrame({"we`ird": [1]})

    assert client.insert_df("db.target", frame, ["we`ird"]) == []
    client.insert("db.target", [(2,)], ["we`ird"], ["Int64"])
    client.insert("db.target", [(3,)], ["we`ird"])
    client.close()
    client.close()

    query = "INSERT INTO `db`.`target` (`we``ird`) VALUES"
    assert raw.insert_dataframe_calls == []
    assert raw.execute_calls[-3:] == [
        (query, ([(1,)],), {}),
        (query, ([(2,)],), {}),
        (query, ([(3,)],), {}),
    ]
    assert raw.disconnects == 1


def test_insert_rejects_mismatched_type_metadata() -> None:
    client = NativeClickHouseClient(FakeNativeClient())

    with pytest.raises(ValueError, match="column_type_names must match"):
        client.insert("db.target", [(1,)], ["value"], ["Int64", "String"])


def test_insert_preserves_binary_strings_with_text_columns() -> None:
    raw = FakeNativeClient()
    raw.client_settings = {"strings_encoding": "utf-8"}
    client = NativeClickHouseClient(raw)

    client.insert(
        "db.target",
        [
            (memoryview(b"\x00\xff"), "Кириллица", 1),
            (bytearray(b"\x01\x80"), "", 2),
            (None, None, 3),
        ],
        ["payload", "label", "row_id"],
        ["Nullable(String)", "Nullable(LowCardinality(String))", "Int64"],
    )

    assert raw.execute_calls[-1] == (
        "INSERT INTO `db`.`target` (`payload`, `label`, `row_id`) VALUES",
        (
            [
                (b"\x00\xff", "Кириллица".encode(), 1),
                (b"\x01\x80", b"", 2),
                (None, None, 3),
            ],
        ),
        {"settings": {"strings_as_bytes": True}},
    )


def test_insert_keeps_text_only_native_path_unchanged() -> None:
    raw = FakeNativeClient()
    client = NativeClickHouseClient(raw)

    client.insert("db.target", [("text",)], ["value"], ["String"])

    assert raw.execute_calls[-1][1:] == (([("text",)],), {})


def test_binary_string_helper_edges() -> None:
    assert _is_string_type(" FixedString(2) ") is True
    assert _is_string_type("Int64") is False
    assert _as_insert_bytes(None, "utf-8") is None
    assert _as_insert_bytes(b"raw", "utf-8") == b"raw"
    assert _as_insert_bytes(memoryview(b"view"), "utf-8") == b"view"
    assert _as_insert_bytes(bytearray(b"mutable"), "utf-8") == b"mutable"
    assert _as_insert_bytes("Кириллица", "utf-8") == "Кириллица".encode()
    assert _as_insert_bytes(7, "utf-8") == 7


def test_stream_parser_supports_native_driver_shapes() -> None:
    metadata = [("value", "Int64")]

    assert _split_first_stream_block([[metadata, (1,)], [(2,), (3,)]]) == (
        [(1,), (2,), (3,)],
        metadata,
    )
    assert _split_first_stream_block([metadata, (1,)]) == ([(1,)], metadata)
    assert _normalize_stream_rows([]) == []
    assert _normalize_stream_rows([[(1,)], [(2,)]]) == [(1,), (2,)]
    assert _is_column_metadata(metadata)
    assert not _is_column_metadata("metadata")


@pytest.mark.parametrize(
    ("call", "error", "message"),
    [
        (lambda: _split_first_stream_block(1), TypeError, "column metadata"),
        (lambda: _split_first_stream_block(["bad"]), ValueError, "column metadata"),
        (lambda: _split_first_stream_block([[(1,)]]), ValueError, "column metadata"),
        (lambda: _normalize_stream_rows(1), TypeError, "row block"),
        (lambda: _normalize_stream_rows([1]), TypeError, "row block"),
    ],
)
def test_stream_parser_rejects_invalid_driver_shapes(
    call: Any,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        call()


def test_stream_requires_context_entry() -> None:
    stream = _NativeDataFrameStream(FakeNativeClient(), "select stream")

    with pytest.raises(RuntimeError, match="column metadata"):
        next(stream._dataframes())
