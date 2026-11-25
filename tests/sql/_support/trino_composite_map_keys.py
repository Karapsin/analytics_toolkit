"""Native composite map casts over actual Trino SDK parameter expressions."""

from __future__ import annotations

import ipaddress
from copy import deepcopy
from dataclasses import dataclass

from sqlglot import exp, parse_one

from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_time_values import (
    NativeTimeConnection,
    NativeTimeCursor,
    NativeTimeDatabase,
)


@dataclass(frozen=True)
class _WireMap:
    pairs: tuple


class CompositeMapDatabase(NativeTimeDatabase):
    def __init__(self, native_type, *, original_rows):
        super().__init__(native_type)
        self.table_rows[self.target] = deepcopy(original_rows)
        self.map_casts = []
        self.cast_types = []

    def decode_cast(self, wire):
        statement = parse_one(self.statements[-1][0], read="trino")
        cast = statement.find(exp.Cast)
        assert cast is not None
        native = cast.args["to"]
        self.map_casts.append(wire)
        self.cast_types.append(native.sql(dialect="trino"))
        return _cast(_decode(parse_one(wire, read="trino")), native)


class _Connection(NativeTimeConnection):
    def cursor(self):
        return _Cursor(self)


class _Cursor(NativeTimeCursor):
    def _select(self, tree, values):
        table = tree.find(exp.Table)
        if table is None or table.db != "information_schema" or table.name != "columns":
            return super()._select(tree, values)
        database = self.connection.database
        identity = table.catalog, values[0], values[1]
        if identity not in database.table_types:
            return None
        record = {
            "table_catalog": identity[0],
            "table_schema": identity[1],
            "table_name": identity[2],
            "column_name": "payload",
            "data_type": database.table_types[identity],
            "is_nullable": "YES",
            "column_default": None,
            "ordinal_position": 1,
        }
        nullable_filters = [
            comparison.expression.this
            for comparison in tree.find_all(exp.EQ)
            if isinstance(comparison.this, exp.Column)
            and comparison.this.name == "is_nullable"
            and isinstance(comparison.expression, exp.Literal)
        ]
        if any(value != record["is_nullable"] for value in nullable_filters):
            return None
        self.result_rows = [tuple(record[column.name] for column in tree.expressions)]
        return None


def native_composite_map_target(monkeypatch, native_type, *, original_rows):
    """Replace acquisition only; retain adapter metadata and write certification."""
    database = CompositeMapDatabase(native_type, original_rows=original_rows)

    def connect(_alias):
        connection = _Connection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database


def _decode(node):
    if isinstance(node, exp.Cast):
        return _cast(_decode(node.this), node.args["to"])
    if isinstance(node, exp.Map):
        keys, values = node.args["keys"].expressions, node.args["values"].expressions
        assert len(keys) == len(values)
        return _WireMap(tuple((_decode(key), _decode(value)) for key, value in zip(keys, values)))
    if isinstance(node, exp.Array):
        return [_decode(item) for item in node.expressions]
    if isinstance(node, (exp.Struct, exp.Tuple)):
        return tuple(_decode(item) for item in node.expressions)
    if isinstance(node, exp.Anonymous) and node.name.casefold() == "row":
        return tuple(_decode(item) for item in node.expressions)
    return _decode_scalar(node)


def _decode_scalar(node):
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Literal):
        return node.to_py()
    if isinstance(node, exp.HexString):
        return bytes.fromhex(node.this)
    if isinstance(node, exp.Neg):
        return -_decode_scalar(node.this)
    message = f"Unsupported native composite-map parameter expression: {node}"
    raise TypeError(message)


def _family(native):
    return str(getattr(native.this, "value", native.this)).casefold()


def _cast(value, native):
    if value is None:
        return None
    family = _family(native)
    if family in {"struct", "row"}:
        fields = [
            field.args["kind"] if isinstance(field, exp.ColumnDef) else field
            for field in native.expressions
        ]
        assert len(value) == len(fields)
        return tuple(_cast(item, field) for item, field in zip(value, fields))
    if family == "array":
        return [_cast(item, native.expressions[0]) for item in value]
    if family == "map":
        return _cast_map(value, native.expressions)
    return _cast_scalar(value, native, family)


def _cast_map(value, fields):
    assert isinstance(value, _WireMap)
    key_type, value_type = fields
    result = {}
    for raw_key, raw_value in value.pairs:
        key = _cast(raw_key, key_type)
        if key is None or key in result:
            message = "Trino rejects duplicate or NULL map keys after native composite casts."
            raise ValueError(message)
        result[key] = _cast(raw_value, value_type)
    return result


def _cast_scalar(value, native, family):
    if family == "ipaddress":
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return str(address)
    if family == "char":
        width = int(native.expressions[0].sql(dialect="trino"))
        return str(value)[:width].rstrip(" ")
    if family == "varchar":
        return str(value)
    if family in {"bigint", "int", "integer", "smallint", "tinyint"}:
        return int(value)
    message = f"Unsupported native composite-map cast: {native}"
    raise TypeError(message)
