"""Real psycopg2 JSON serialization with stateful existing/new target storage."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import ClassVar

from analytics_toolkit.sql.dml.transfer.runtime import retry as transfer_retry
from sqlglot import exp, parse_one
from sqlglot.dialects.postgres import Postgres

from tests.sql._support.load_encoded_values import _EncodedCursor
from tests.sql._support.load_table import load_df_module
from tests.sql._support.transfer_not_null import (
    TransferConnection,
    TransferCursor,
    TransferDatabase,
)


class _StandalonePsycopgPostgres(Postgres):
    class Tokenizer(Postgres.Tokenizer):
        # Unprepared psycopg adapters use legacy backslash escaping. Interpret
        # their actual SQL bytes in that mode, without changing the serializer.
        STRING_ESCAPES: ClassVar[list[str]] = ["'", "\\"]


class _UndefinedFixtureTypeError(ValueError):
    pgcode = "42704"


def _json_catalog_type_exists(native):
    if native.this == exp.DataType.Type.ARRAY:
        return _json_catalog_type_exists(native.expressions[0])
    if native.this in {exp.DataType.Type.JSON, exp.DataType.Type.JSONB}:
        return True
    identity = native.args.get("kind")
    parts = [identity.this, identity.expression] if isinstance(identity, exp.Dot) else [identity]
    if not all(isinstance(part, exp.Identifier) for part in parts):
        return False
    names = tuple(part.name if part.args.get("quoted") else part.name.lower() for part in parts)
    return names in {("json",), ("jsonb",), ("pg_catalog", "json"), ("pg_catalog", "jsonb")}


class JsonDatabase(TransferDatabase):
    def __init__(self, native_type, *, exists):
        super().__init__(
            "gp", columns=("payload",), not_null=(), target_types={"payload": native_type}
        )
        self.dialect = _StandalonePsycopgPostgres
        if not exists:
            self.tables.clear()
        self.bound_rows = []
        self.serialized_rows = []
        self.sql_null_rows = []
        self.opens = 0
        self.lost_insert_acknowledgements = 0
        self.rejected_type_creates = []

    def execute(self, connection, statement, params=None):
        tree = parse_one(self._ddl_schema_prefix(statement), read=self.dialect)
        if isinstance(tree, exp.Create) and any(
            not _json_catalog_type_exists(column.kind) for column in tree.this.expressions
        ):
            self.commands.append(statement)
            self.rejected_type_creates.append(statement)
            message = "The requested type does not exist in the fixture catalog."
            raise _UndefinedFixtureTypeError(message)
        return super().execute(connection, statement, params)

    def _insert(self, tree, identity, params):
        assert params is None
        assert isinstance(tree.expression, exp.Values)
        names = [column.name for column in tree.this.expressions]
        schema = self.tables[identity].schema
        expressions = [row.expressions for row in tree.expression.expressions]
        rows = [
            tuple(self.decode_value(value, schema[name]) for value, name in zip(values, names))
            for values in expressions
        ]
        candidate = deepcopy(self.tables[identity])
        required = candidate.not_null
        if any(
            name in required and isinstance(uncast(value), exp.Null)
            for values in expressions
            for name, value in zip(names, values)
        ):
            message = "Server rejected SQL NULL in a NOT NULL JSON column."
            raise ValueError(message)
        # Decoding the non-NULL JSON document 'null' also yields Python None.
        # Apply the server constraint from SQL syntax before that distinction is
        # lost, while the ordinary store still checks omitted required columns.
        candidate.not_null = required - set(names)
        candidate.insert(names, rows)
        candidate.not_null = required
        self.tables[identity] = candidate
        self.serialized_rows.extend(
            tuple(value.this if isinstance(value, exp.Literal) else None for value in values)
            for values in expressions
        )
        self.sql_null_rows.extend(
            tuple(isinstance(uncast(value), exp.Null) for value in values) for values in expressions
        )

    def decode_value(self, node, native_type):
        if isinstance(node, exp.Cast):
            return self.decode_value(node.this, native_type)
        if isinstance(node, exp.Null):
            return None
        if native_type.endswith("[]"):
            return self.decode_array(node, native_type.rstrip("[]"))
        assert isinstance(node, exp.Literal)
        assert node.is_string
        value = json.loads(node.this, parse_constant=reject_nonfinite)
        if native_type.lower() == "jsonb" and contains_nul(value):
            message = "Server rejected a NUL escape in JSONB."
            raise ValueError(message)
        return value

    def decode_array(self, node, native_type, *, literal_array=True):
        node = uncast(node)
        if isinstance(node, exp.Null):
            return None
        if isinstance(node, exp.Array):
            nested = any(isinstance(uncast(item), exp.Array) for item in node.expressions)
            return [
                self.decode_array(item, native_type, literal_array=nested)
                for item in node.expressions
            ]
        assert isinstance(node, exp.Literal)
        if literal_array:
            assert node.this in {"{}", "{NULL}"}
            return [] if node.this == "{}" else [None]
        return self.decode_value(node, native_type)


def uncast(node):
    while isinstance(node, exp.Cast):
        node = node.this
    return node


def reject_nonfinite(_value):
    message = "Server rejected a non-finite JSON number."
    raise ValueError(message)


def contains_nul(value):
    if isinstance(value, dict):
        return any(contains_nul(key) or contains_nul(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_nul(item) for item in value)
    return isinstance(value, str) and "\x00" in value


class JsonConnection(TransferConnection):
    encoding = "UTF8"

    @property
    def bound_rows(self):
        return self.database.bound_rows

    def cursor(self):
        return JsonCursor(self)

    def commit(self):
        super().commit()
        if getattr(self, "insert_pending", False):
            self.insert_pending = False
            if self.database.lost_insert_acknowledgements:
                self.database.lost_insert_acknowledgements -= 1
                message = "Lost acknowledgement after committing JSON insert."
                raise ConnectionError(message)


class JsonCursor(TransferCursor):
    # Keep execute_values itself, Json adapters and their dumps/getquoted paths
    # real. Only the cursor's network boundary is replaced.
    mogrify = _EncodedCursor.mogrify

    def execute(self, statement, params=None):
        text = statement.decode("utf-8") if isinstance(statement, bytes) else statement
        super().execute(text, params)
        self.connection.insert_pending = isinstance(parse_one(text, read="postgres"), exp.Insert)


def json_target(monkeypatch, native_type, *, exists=True):
    database = JsonDatabase(native_type, exists=exists)

    def connect(_key):
        database.opens += 1
        return JsonConnection(database)

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    monkeypatch.setattr(transfer_retry, "get_sql_connection", connect)
    return database
