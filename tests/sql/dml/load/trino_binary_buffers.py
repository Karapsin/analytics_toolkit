"""Valid native binary buffers work with every supported Trino formatter."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.load import AmbiguousTableLoadError

from tests.sql._support.trino_binary_buffers import binary_target, use_actual_formatter


def _load(frame, **options):
    return sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        trino_insert_chunk_size=2,
        **options,
    )


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_trino_binary_buffer_memoryview_load_uses_supported_native_parameters(
    monkeypatch, sdk, explicit
):
    use_actual_formatter(monkeypatch, sdk)
    database = binary_target(monkeypatch, "VARBINARY", "X'00ff'", b"\x00\xff")
    original_rows = list(database.rows)
    value = memoryview(b"\x00\xff")
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    options = {"table_schema": {"payload": "VARBINARY"}} if explicit else {}

    try:
        count = _load(frame, **options)
    except AmbiguousTableLoadError:
        assert database.rows == original_rows
        raise

    assert count == 1
    assert database.rows == [b"\x00\xff"]
    assert database.mutations == ["clear", "insert"]
    assert frame.iloc[0, 0] is value
    assert value.tobytes() == b"\x00\xff"
    assert database.statements[-1][2] == ["X'00ff'"]
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"\x00\xff", b"\x00\xff"),
        (bytearray(b"\x00\xff"), b"\x00\xff"),
        (memoryview(b""), b""),
        (memoryview(b"\x00A\xffB")[::2], b"\x00\xff"),
        (bytearray(), b""),
        (None, None),
    ],
    ids=["bytes", "bytearray", "empty-view", "noncontiguous-view", "empty-bytearray", "null"],
)
def test_trino_binary_buffer_scalar_cells_keep_exact_bytes_and_nulls(
    monkeypatch, sdk, value, expected
):
    use_actual_formatter(monkeypatch, sdk)
    wire = "NULL" if expected is None else f"X'{expected.hex()}'"
    database = binary_target(monkeypatch, "VARBINARY", wire, expected)
    frame = pd.DataFrame({"payload": pd.Series([None, value, None], dtype=object)})

    assert _load(frame) == 3

    assert database.rows == [None, expected, None]
    assert database.mutations == ["clear", "insert", "insert"]
    assert database.statements[-2][2] == ["NULL", wire]
    assert database.statements[-1][2] == ["NULL"]
    assert frame.iloc[1, 0] is value
    if value is not None:
        assert bytes(value) == expected
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize(
    "case",
    [
        (
            "ARRAY(VARBINARY)",
            [memoryview(b"\x00\xff"), None, bytearray(b"\x01\x00")],
            "ARRAY[X'00ff',NULL,X'0100']",
            [b"\x00\xff", None, b"\x01\x00"],
            [b"original"],
        ),
        (
            "ROW(blob VARBINARY, label VARCHAR)",
            (memoryview(b"\x00\xff"), "kept"),
            "ROW(X'00ff','kept')",
            (b"\x00\xff", "kept"),
            (b"original", "seed"),
        ),
        (
            "MAP(VARCHAR, VARBINARY)",
            {"blob": memoryview(b"\x00\xff"), "empty": bytearray(), "missing": None},
            "MAP(ARRAY['blob','empty','missing'], ARRAY[X'00ff',X'',NULL])",
            {"blob": b"\x00\xff", "empty": b"", "missing": None},
            {"seed": b"original"},
        ),
        (
            "MAP(VARBINARY, VARCHAR)",
            {memoryview(b"\x00\xff"): "kept"},
            "MAP(ARRAY[X'00ff'], ARRAY['kept'])",
            {b"\x00\xff": "kept"},
            {b"original": "seed"},
        ),
    ],
    ids=["array", "row", "map-value", "map-key"],
)
def test_trino_binary_buffer_nested_cells_reach_the_actual_sdk_recursively(monkeypatch, sdk, case):
    native, value, wire, expected, original = case
    use_actual_formatter(monkeypatch, sdk)
    database = binary_target(monkeypatch, native, wire, expected, original_value=original)
    frame = pd.DataFrame({"payload": pd.Series([None, value, None], dtype=object)})

    assert _load(frame, table_schema={"payload": native}) == 3

    assert database.rows == [None, expected, None]
    assert database.mutations == ["clear", "insert", "insert"]
    assert database.statements[-2][2] == ["NULL", wire]
    assert database.statements[-1][2] == ["NULL"]
    assert frame.iloc[1, 0] is value
    assert database.native_type == native
    assert all(connection.close_calls == 1 for connection in database.opened)
