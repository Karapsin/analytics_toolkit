"""Composite native key equality must be certified before clearing a Trino target."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from sqlglot import exp, parse_one

from tests.sql._support.trino_composite_map_keys import native_composite_map_target

_ROW_TYPES = {
    "ip-char": "ROW(address IPADDRESS, label CHAR(2))",
    "char-ip": "ROW(label CHAR(2), address IPADDRESS)",
    "nested-ip-char": "ROW(marker BIGINT, detail ROW(address IPADDRESS, label CHAR(2)))",
    "nested-char-ip": "ROW(detail ROW(label CHAR(2), address IPADDRESS), marker BIGINT)",
}
_SHAPES = tuple(_ROW_TYPES)


def _key(shape, address, label):
    if shape == "ip-char":
        return address, label
    if shape == "char-ip":
        return label, address
    if shape == "nested-ip-char":
        return 7, (address, label)
    return (label, address), 7


def _native(shape):
    return f"MAP({_ROW_TYPES[shape]}, VARCHAR)"


def _load(frame, **options):
    return sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


def _write_statements(database):
    mutations = (exp.Delete, exp.TruncateTable, exp.Create, exp.Insert, exp.Drop, exp.Alter)
    return [
        statement
        for statement in database.statements
        if isinstance(parse_one(statement[0], read="trino"), mutations)
    ]


def _load_preserving_existing_target(frame, database, original_rows, options):
    try:
        _load(frame, **options)
    except Exception as error:
        diagnostic = {
            "error": repr(error),
            "original_rows": original_rows,
            "rows": database.rows,
            "mutations": database.mutations,
            "write_statements": _write_statements(database),
        }
        assert database.rows == original_rows, repr(diagnostic)
        assert database.mutations == [], repr(diagnostic)
        assert _write_statements(database) == [], repr(diagnostic)
        raise
    pytest.fail("Invalid composite map keys passed destination certification")


@pytest.mark.parametrize("shape", _SHAPES)
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_trino_composite_native_map_keys_reject_before_truncate(monkeypatch, shape, explicit):
    native = _native(shape)
    original_rows = [{_key(shape, "198.51.100.9", "z"): "preserved"}]
    database = native_composite_map_target(monkeypatch, native, original_rows=original_rows)
    payload = {
        _key(shape, "192.0.2.1", "a"): "first",
        _key(shape, "::ffff:192.0.2.1", "a "): "second",
    }
    original_payload = deepcopy(payload)
    frame = pd.DataFrame({"payload": pd.Series([payload], dtype=object)})
    options = {"table_schema": {"payload": native}} if explicit else {}
    assert len(payload) == 2

    with pytest.raises(ValueError, match=r"(?i)map|key|IPADDRESS|unique|duplicate"):
        _load_preserving_existing_target(frame, database, original_rows, options)

    diagnostic = {
        "rows": database.rows,
        "mutations": database.mutations,
        "write_statements": _write_statements(database),
        "native_type": database.native_type,
    }
    assert database.rows == original_rows, repr(diagnostic)
    assert database.mutations == []
    assert _write_statements(database) == []
    assert database.map_casts == []
    assert database.native_type == native
    assert database.table_types[database.target] == native
    assert payload == original_payload
    assert frame.iloc[0, 0] is payload
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("shape", _SHAPES)
@pytest.mark.parametrize("different", ["label", "address"])
def test_trino_composite_native_map_keys_keep_distinct_rows_and_bare_return(
    monkeypatch, shape, different
):
    native = _native(shape)
    original_rows = [{_key(shape, "198.51.100.9", "z"): "preserved"}]
    database = native_composite_map_target(monkeypatch, native, original_rows=original_rows)
    address = "::ffff:192.0.2.1" if different == "label" else "::ffff:192.0.2.2"
    label = "b" if different == "label" else "a "
    payload = {
        _key(shape, "192.0.2.1", "a"): "first",
        _key(shape, address, label): "second",
    }
    original_payload = deepcopy(payload)
    frame = pd.DataFrame({"payload": pd.Series([payload], dtype=object)})

    result = _load(frame)

    assert isinstance(result, int)
    assert not isinstance(result, bool)
    assert result == 1
    stored_address = "192.0.2.1" if different == "label" else "192.0.2.2"
    stored_label = "b" if different == "label" else "a"
    expected = {
        _key(shape, "192.0.2.1", "a"): "first",
        _key(shape, stored_address, stored_label): "second",
    }
    assert database.rows == [expected]
    assert database.mutations == ["clear", "insert"]
    assert database.cast_types == [exp.DataType.build(native, dialect="trino").sql(dialect="trino")]
    assert len(database.map_casts) == 1
    writes = _write_statements(database)
    assert len(writes) == 2
    insert = next(statement for statement in writes if "INSERT INTO" in statement[0])
    assert insert[1] == [payload]
    assert database.native_type == native
    assert database.table_types[database.target] == native
    assert payload == original_payload
    assert frame.iloc[0, 0] is payload
    assert database.opened
    assert all(connection.close_calls == 1 for connection in database.opened)
