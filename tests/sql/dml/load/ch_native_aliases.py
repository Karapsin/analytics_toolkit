"""Valid ClickHouse storage aliases retain their native raw driver contracts."""

from __future__ import annotations

import json
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from clickhouse_driver.errors import TypeMismatchError

from tests.sql._support.ch_native_aliases import alias_target, http_factory_available
from tests.sql.backends.ch._support.network_transport import encode_normalized

_RING = [(1.25, 2.5), (3.5, 4.75), (1.25, 2.5)]
_POLYGON = [_RING]
_MULTIPOLYGON = [_POLYGON]


def alias_values(native_type, transport):
    if native_type == "SimpleAggregateFunction(sum, UInt64)":
        return [7, 2**64 - 1]
    if native_type.startswith("Nested("):
        return (
            [[(7, "a"), (2**64 - 1, "b")], []]
            if transport == "native"
            else [[{"x": 7, "y": "a"}, {"x": 2**64 - 1, "y": "b"}], []]
        )
    if native_type == "Object('json')":
        return [{"a": 1}, {"a": 2, "b": "value"}]
    return deepcopy(
        {
            "Point": [(1.25, 2.5), (-3.5, 4.75)],
            "Ring": [_RING, []],
            "Polygon": [_POLYGON, []],
            "MultiPolygon": [_MULTIPOLYGON, []],
        }[native_type]
    )


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("mode", ["append", "truncate_insert"])
@pytest.mark.parametrize(
    "native_type",
    [
        "SimpleAggregateFunction(sum, UInt64)",
        "Point",
        "Ring",
        "Polygon",
        "MultiPolygon",
        "Nested(x UInt64, y String)",
        "Object('json')",
    ],
    ids=["simple-aggregate", "point", "ring", "polygon", "multipolygon", "nested", "object"],
)
def test_ch_native_aliases_keep_public_schema_values_and_real_driver_bytes(
    monkeypatch,
    transport,
    explicit,
    mode,
    native_type,
):
    values = alias_values(native_type, transport)
    store = alias_target(monkeypatch, transport, native_type, values[0])
    seed = deepcopy(store.rows)
    frame = pd.DataFrame({"payload": pd.Series(deepcopy(values), dtype=object)})
    # pandas deep copies still share object cells, so retain separate containers.
    original = pd.DataFrame({"payload": pd.Series(deepcopy(values), dtype=object)})
    options = {"table_schema": {"payload": native_type}} if explicit else {}

    if transport == "http" and not http_factory_available(native_type):
        with pytest.raises(ValueError, match=r"ClickHouse|authoritative|[Uu]nsupported"):
            sql.load_df(
                "ch",
                "sandbox.events",
                frame,
                write_mode=mode,
                retry_cnt=1,
                timeout_increment=0,
                **options,
            )
        assert store.rows == seed
        assert store.submissions == []
        assert store.wire_columns == []
    else:
        # This is the installed driver's unmodified serializer, before any public
        # helper runs. It proves the raw type and representative values are valid.
        source_wire = encode_normalized(
            native_type,
            deepcopy(values),
            transport,
            settings=store.settings,
        )
        expected_values = (
            [json.dumps(value) for value in values]
            if native_type == "Object('json')" and transport == "native"
            else values
        )
        if native_type.startswith("Nested(") and transport == "http":
            expected_values = [
                [{"x": item["x"], "y": item["y"].encode("utf-8")} for item in value]
                for value in values
            ]
        expected_wire = (
            encode_normalized(
                native_type, deepcopy(expected_values), transport, settings=store.settings
            )
            if native_type == "Object('json')" and transport == "native"
            else source_wire
        )
        try:
            count = sql.load_df(
                "ch",
                "sandbox.events",
                frame,
                write_mode=mode,
                retry_cnt=1,
                timeout_increment=0,
                **options,
            )
        except ValueError:
            assert store.rows == seed
            assert store.submissions == []
            raise
        assert count == len(values)
        assert store.bound_rows == [(value,) for value in expected_values]
        assert store.wire_columns == [("payload", native_type, expected_wire)]
        expected_rows = [{"payload": value} for value in expected_values]
        assert store.rows == (seed if mode == "append" else []) + expected_rows
        assert [kind for kind, _detail in store.submissions] == (
            ["INSERT"] if mode == "append" else ["CLEAR", "INSERT"]
        )

    assert store.schema == {"payload": native_type}
    assert store.create_calls == []
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize(
    ("native_type", "valid", "invalid", "codec_error"),
    [
        ("SimpleAggregateFunction(sum, UInt64)", 7, -1, TypeMismatchError),
        ("Point", (1.25, 2.5), (1.25,), IndexError),
        ("Nested(x UInt64, y String)", [(7, "a")], [(-1, "b")], TypeMismatchError),
        ("Object('json')", {"a": 1}, {"a": b"unsupported"}, TypeError),
    ],
    ids=["simple-aggregate", "point", "nested", "object"],
)
def test_ch_native_aliases_invalid_last_value_preserves_live_rows_before_encoding(
    monkeypatch,
    native_type,
    valid,
    invalid,
    codec_error,
):
    store = alias_target(monkeypatch, "native", native_type, valid)
    seed = deepcopy(store.rows)
    values = [deepcopy(valid), deepcopy(invalid)]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = pd.DataFrame({"payload": pd.Series(deepcopy(values), dtype=object)})

    # The actual installed serializer proves why the complete input cannot be
    # written, including the last row after a valid first row.
    with pytest.raises(codec_error):
        encode_normalized(native_type, deepcopy(values), "native", settings=store.settings)
    with pytest.raises(
        ValueError, match=r"ClickHouse|authoritative|[Ii]nvalid|[Ee]xceed|[Cc]ertify"
    ):
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert store.rows == seed
    assert store.submissions == []
    assert store.wire_columns == []
    assert store.create_calls == []
    assert store.schema == {"payload": native_type}
    pd.testing.assert_frame_equal(frame, original)
