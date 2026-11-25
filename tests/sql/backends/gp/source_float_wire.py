"""Real network float payloads, metadata identity and malformed-wire rejection."""

from __future__ import annotations

import math
import struct

import pytest
from analytics_toolkit.sql.backends.gp.source_float_binary import (
    SourceFloatWireError,
    decode_float,
    decode_float_array,
)
from analytics_toolkit.sql.backends.gp.source_float_wire import execute_exact_source_query
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.native_scalars import ExactArray, NativeFloatNaN


@pytest.mark.parametrize("width", [4, 8])
def test_gp_send_float_preserves_zero_sign_and_native_nonfinite_identity(width):
    code = "!f" if width == 4 else "!d"
    for value in [-0.0, 1.23456789012345, math.inf, -math.inf, math.nan]:
        wire = struct.pack(code, value)
        decoded = decode_float(memoryview(wire), width)
        assert struct.pack(code, decoded) == wire
        if math.isnan(value):
            assert isinstance(decoded, NativeFloatNaN)
    assert decode_float(None, width) is None


@pytest.mark.parametrize("wire", [b"", b"123", "12345678", object()])
def test_gp_send_float_rejects_unverifiable_cells(wire):
    with pytest.raises(SourceFloatWireError) as error:
        decode_float(wire, 8)
    assert error.value.analytics_toolkit_sql_retry_safe is False


def test_gp_array_send_keeps_bounds_null_and_empty_shape():
    wire = struct.pack("!iiiiii", 1, 1, 701, 2, -3, 8)
    wire += struct.pack("!d", -0.0) + struct.pack("!i", -1)
    actual = decode_float_array(memoryview(wire), 8, 701)
    assert actual == ExactArray((-0.0, None), (2,), (-3,))
    assert struct.pack("!d", actual.values[0]) == struct.pack("!d", -0.0)
    assert decode_float_array(struct.pack("!iii", 0, 0, 701), 8, 701) == ExactArray((), (), ())
    assert decode_float_array(None, 8, 701) is None


@pytest.mark.parametrize(
    "wire",
    [
        b"",
        "not bytes",
        struct.pack("!iii", 7, 0, 701),
        struct.pack("!iii", 0, 2, 701),
        struct.pack("!iii", 0, 0, 700),
        struct.pack("!iiiii", 1, 0, 701, -1, 1),
        struct.pack("!iiiii", 1, 0, 701, 1000000, 1),
        struct.pack("!iiiiii", 1, 0, 701, 1, 1, -1),
        struct.pack("!iiiiii", 1, 0, 701, 1, 1, 7) + b"1234567",
        struct.pack("!iii", 0, 0, 701) + b"trailing",
    ],
)
def test_gp_array_send_rejects_invalid_header_lengths_and_null_flags(wire):
    with pytest.raises(SourceFloatWireError):
        decode_float_array(wire, 8, 701)


class WireCursor:
    def __init__(self, connection, description=None):
        self.connection = connection
        self.description = description
        self.rows = []
        self.closed = False

    def execute(self, query, params=None):
        self.connection.queries.append((query, params))
        if "pg_catalog.pg_type" in query:
            self.rows = list(self.connection.domain_rows)
        elif "WHERE 1 = 0" in query:
            self.description = self.connection.description
        else:
            self.rows = list(self.connection.rows)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchmany(self, size=1):
        rows, self.rows = self.rows[:size], self.rows[size:]
        return rows

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        self.closed = True


class WireConnection:
    def __init__(self, description, rows, domains=()):
        self.description = description
        self.rows = rows
        self.domain_rows = domains
        self.queries = []
        self.cursors = []

    def cursor(self):
        cursor = WireCursor(self)
        self.cursors.append(cursor)
        return cursor


def test_gp_float_projection_restores_duplicate_labels_and_all_fetch_shapes():
    description = [("same", 701), ("same", 23)]
    connection = WireConnection(description, [(struct.pack("!d", 1.5), 7)] * 4)
    raw = connection.cursor()
    cursor = execute_exact_source_query(connection, raw, "SELECT a, b FROM original;")

    assert cursor.description is description
    assert cursor.fetchone() == (1.5, 7)
    assert cursor.fetchmany() == [(1.5, 7)]
    assert list(cursor) == [(1.5, 7), (1.5, 7)]
    assert cursor.fetchone() is None
    cursor.close()
    assert raw.closed
    assert len(connection.queries) == 2
    assert "FROM (\nSELECT a, b FROM original\n)" in connection.queries[-1][0]
    assert 'AS "_atk_source"("_atk_0", "_atk_1")' in connection.queries[-1][0]


def test_gp_float_domain_resolves_actual_base_oid_before_binary_projection():
    description = [("private", 98765)]
    connection = WireConnection(description, [(struct.pack("!d", 2.5),)], [("d", 701, 0)])
    cursor = execute_exact_source_query(
        connection,
        connection.cursor(),
        "SELECT domain_column FROM original",
        source_description=description,
    )
    assert cursor.fetchall() == [(2.5,)]
    assert connection.queries[0][1] == (98765,)
    assert connection.cursors[1].closed
    assert "pg_catalog.float8send" in connection.queries[1][0]


def test_gp_known_nonfloat_metadata_keeps_original_query_without_probe():
    connection = WireConnection([("x", 23)], [(1,)])
    raw = connection.cursor()
    cursor = execute_exact_source_query(
        connection, raw, "SELECT 1", source_columns=[SourceColumn("x", "INTEGER")]
    )
    assert cursor is raw
    assert cursor.fetchall() == [(1,)]
    assert connection.queries == [("SELECT 1", None)]


@pytest.mark.parametrize(
    "domain_rows",
    [[], [("d",)], [("d", 98765, 0)], [("b", 0, 1022)], [("c", 0, 0)]],
    ids=["missing-catalog", "incomplete-catalog", "cyclic-domain", "nested-array", "composite"],
)
def test_gp_unverifiable_float_source_catalog_rejects_before_data_execution(domain_rows):
    connection = WireConnection([("x", 98765)], [(b"private-unselected-value",)], domain_rows)
    with pytest.raises(SourceFloatWireError) as error:
        execute_exact_source_query(connection, connection.cursor(), "SELECT private_source")
    assert "private-unselected-value" not in str(error.value)
    assert not any("pg_catalog.float8send" in query for query, _params in connection.queries)
    assert all(cursor.closed for cursor in connection.cursors[1:])


@pytest.mark.parametrize("catalog", [("b", 0, 0), ("b", 0, 23)])
def test_gp_catalog_proven_nonfloat_native_values_keep_original_execution(catalog):
    connection = WireConnection([("x", 98765)], [("native-value",)], [catalog])
    cursor = execute_exact_source_query(connection, connection.cursor(), "SELECT opaque_source")
    assert cursor.fetchall() == [("native-value",)]
    assert connection.queries[-1] == ("SELECT opaque_source", None)


def test_gp_catalog_float_array_uses_binary_element_oid_and_original_metadata():
    wire = struct.pack("!iiiiii", 1, 0, 701, 1, -1, 8) + struct.pack("!d", 2.5)
    header = [("x", 98765)]
    connection = WireConnection(header, [(wire,)], [("b", 0, 701)])
    cursor = execute_exact_source_query(connection, connection.cursor(), "SELECT native_array")
    assert cursor.fetchall() == [(ExactArray((2.5,), (1,), (-1,)),)]
    assert cursor.description is header
    assert "pg_catalog.array_send" in connection.queries[-1][0]


def test_gp_float_cursor_rejects_column_shape_change_without_private_value_echo():
    connection = WireConnection([("x", 701)], [(b"private-shape", 1)])
    cursor = execute_exact_source_query(connection, connection.cursor(), "SELECT original")
    with pytest.raises(SourceFloatWireError, match="column count") as error:
        cursor.fetchmany(1)
    assert "private-shape" not in str(error.value)


def test_gp_missing_metadata_fails_without_selected_value_execution():
    connection = WireConnection(None, [])
    with pytest.raises(SourceFloatWireError, match="complete native column metadata"):
        execute_exact_source_query(connection, connection.cursor(), "SELECT original")
    assert len(connection.queries) == 1
    assert "WHERE 1 = 0" in connection.queries[0][0]
