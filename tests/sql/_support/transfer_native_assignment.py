"""Real GP parameter adaptation over native assignment and transfer SQL state."""

from __future__ import annotations

import ipaddress
from copy import deepcopy

from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.dml.transfer.flow import (
    adaptive_input,
    api,
    attempt,
    finalize,
    keyed_pipeline,
)
from analytics_toolkit.sql.dml.transfer.runtime import retry as transfer_retry
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch
from sqlglot import exp, parse_one

from tests.sql._support.created_gp_relations import relation_catalog_result
from tests.sql._support.gp_cidr_casts import SHORT_DOMAIN
from tests.sql._support.gp_inet_casts import InetDatabase
from tests.sql._support.gp_temporal_values import TemporalConnection
from tests.sql._support.load_column_state import ExistingColumnStore

CHECKED_DOMAIN = '"fixture"."checked_text"'


class NativeInputError(ValueError):
    sqlstate = "22000"


class NativeTransferDatabase(InetDatabase):
    def __init__(self, *, rows, target_schema, source_types=None, exists=True):
        super().__init__("CIDR")
        self.source_columns = list(target_schema)
        self.source_types = source_types or dict.fromkeys(self.source_columns, "Nullable(String)")
        self.source_rows = rows
        target = ExistingColumnStore("gp", self.source_columns, not_null=())
        target.schema = dict(target_schema)
        target.rows = [{name: self.seed(native) for name, native in target_schema.items()}]
        self.tables = {("sandbox", "events"): target} if exists else {}
        self.reads = []
        self.source_opens = 0
        self.fail_target_ack = 0
        self.fail_secondary_ack = 0
        self.source_unavailable = False
        self.cast_values = []
        self.assignment_values = []
        self.parameter_catalog_reads = []
        self.private_value_attempts = []
        self.next_relation_oid = 10000
        self.namespace_oids = {}

    def execute(self, connection, statement, params=None):
        catalog = relation_catalog_result(self, connection, statement, params)
        if catalog is not None:
            return catalog
        return super().execute(connection, statement, params)

    def metadata(self, statement, params):
        if "a.atttypid, a.attndims" in statement and "a.attname = ANY" in statement:
            schema, relation, names = params
            table = self.tables[(schema, relation)]
            oids = {"CIDR[]": 1001, SHORT_DOMAIN + "[]": 1003}
            self.parameter_catalog_reads.append((schema, relation, tuple(names)))
            return [(name, oids[table.schema[name]], 1) for name in names]
        if "t.typbasetype" in statement:
            catalog = {
                1001: (0, 1002, "cidr[]", "pg_catalog._cidr"),
                1002: (0, 0, "cidr", "pg_catalog.cidr"),
                1003: (0, 1004, "fixture.short_text[]", "fixture._short_text"),
                1004: (1005, 0, "fixture.short_text", "fixture.short_text"),
                1005: (0, 0, "character varying", "pg_catalog.varchar"),
            }
            return [catalog[params[0]]]
        return super().metadata(statement, params)

    @classmethod
    def seed(cls, native):
        if native in {"INT", "INTEGER", "BIGINT"}:
            return 99
        if native in {SHORT_DOMAIN, CHECKED_DOMAIN, "TEXT", "VARCHAR"}:
            return "ok"
        if native.upper() == "MACADDR":
            return bytes.fromhex("08002b010203")
        if native.endswith("[]"):
            return [cls.seed(native[:-2])]
        return cls.assign("198.51.100.0/24", native)

    @staticmethod
    def assign(value, native, *, explicit=False):
        if value is None:
            return None
        if native.endswith("[]"):
            return [NativeTransferDatabase.assign(item, native[:-2]) for item in value]
        strings = {"TEXT", "VARCHAR", "CHARACTER VARYING", "pg_catalog.varchar"}
        if native in strings | {"INT", "INTEGER", "BIGINT"}:
            return int(value) if native in {"INT", "INTEGER", "BIGINT"} else str(value)
        domains = {SHORT_DOMAIN, "fixture.short_text", CHECKED_DOMAIN, "fixture.checked_text"}
        if native in domains:
            text = str(value)[:3] if explicit else str(value)
            checked = native in {CHECKED_DOMAIN, "fixture.checked_text"}
            if len(text) > 3 or (checked and text == "bad"):
                message = f"Native domain assignment rejected private value {value!r}."
                raise NativeInputError(message)
            return text
        if native.upper() == "MACADDR":
            if isinstance(value, bytes):
                assert len(value) == 6
            return (
                value
                if isinstance(value, bytes)
                else bytes.fromhex(str(value).replace(":", "").replace("-", ""))
            )
        assert native in {"CIDR", "cidr", "pg_catalog.cidr"}
        try:
            return ipaddress.ip_network(str(value), strict=True)
        except ValueError:
            message = f"Native CIDR assignment rejected private value {value!r}."
            raise NativeInputError(message) from None

    @classmethod
    def decode_value(cls, node, native_type):
        if isinstance(node, exp.Cast):
            converted = cls.decode_value(node.this, node.args["to"].sql(dialect="postgres"))
            return cls.assign(converted, native_type)
        if isinstance(node, exp.Null):
            return None
        if native_type.endswith("[]") and isinstance(node, exp.Literal):
            assert node.this in {"{}", "{NULL}"}
            return [] if node.this == "{}" else [None]
        if isinstance(node, exp.Array):
            return [cls.decode_value(item, native_type[:-2]) for item in node.expressions]
        assert isinstance(node, exp.Literal)
        return cls.assign(node.this if node.is_string else int(node.this), native_type)

    def _insert(self, tree, identity, params):
        if isinstance(tree.expression, exp.Values):
            self.private_value_attempts.append(identity)
            return super()._insert(tree, identity, params)
        assert params is None
        names = [column.name for column in tree.this.expressions]
        rows, _columns = self.select(tree.expression)
        schema = self.tables[identity].schema
        assigned = [
            tuple(self.assign(value, schema[name]) for name, value in zip(names, row))
            for row in rows
        ]
        self.assignment_values.append((identity, deepcopy(assigned)))
        candidate = deepcopy(self.tables[identity])
        candidate.insert(names, assigned)
        self.tables[identity] = candidate
        if identity != ("sandbox", "events"):
            self.stage_inserts.append((identity, deepcopy(assigned)))
        return None

    def value(self, node, row):
        if isinstance(node, exp.Cast):
            native = node.args["to"].sql(dialect="postgres")
            value = self.assign(self.value(node.this, row), native, explicit=True)
            self.cast_values.append((native, value))
            return value
        return super().value(node, row)


class NativeTransferConnection(TemporalConnection):
    def __init__(self, database):
        super().__init__(database)
        self.alias = "gp"

    def commit(self):
        database = self.database
        statement = database._ddl_schema_prefix(database.commands[-1]) if database.commands else ""
        tree = parse_one(statement, read="postgres") if statement else None
        target_insert = isinstance(tree, exp.Insert) and database.identity(tree.this) == (
            "sandbox",
            "events",
        )
        secondary_insert = (
            isinstance(tree, exp.Insert)
            and not isinstance(tree.expression, exp.Values)
            and database.identity(tree.this) != ("sandbox", "events")
            and database.table(tree.this).schema.get("payload")
            in {SHORT_DOMAIN, "fixture.short_text"}
        )
        super().commit()
        if secondary_insert and database.fail_secondary_ack:
            database.fail_secondary_ack -= 1
            database.source_unavailable = True
            database.source_rows = [("poisoned after raw checkpoint certification",)]
            message = "Lost acknowledgement after committing native final-stage population."
            raise ConnectionError(message)
        if target_insert and database.fail_target_ack:
            database.fail_target_ack -= 1
            database.source_unavailable = True
            database.source_rows = [("poisoned after certification",)]
            message = "Lost acknowledgement after committing native transfer publication."
            raise ConnectionError(message)


def install_native_transfer(
    monkeypatch,
    *,
    rows,
    target_schema,
    source_types=None,
    exists=True,
    **source_identity,
):
    source_key = source_identity.pop("source_key", "ch")
    source_backend = source_identity.pop("source_backend", "ch")
    assert not source_identity
    database = NativeTransferDatabase(
        rows=rows, target_schema=target_schema, source_types=source_types, exists=exists
    )

    def connect(alias):
        if alias == source_key:
            database.source_opens += 1
            if database.source_unavailable:
                message = "Certified native transfer reopened its mutable source."
                raise AssertionError(message)
        connection = NativeTransferConnection(database)
        connection.alias = alias
        database.opened.append(connection)
        return connection

    def inspect(_backend, _connection, _query):
        return [SourceColumn(name, database.source_types[name]) for name in database.source_columns]

    def read(_key, _backend, _ref, query, *_args, **_kwargs):
        database.reads.append(query)
        tree = parse_one(query, read="postgres" if source_backend == "gp" else "clickhouse")
        for row in database.source_rows:
            if database.matches(tree.args.get("where"), dict(zip(database.source_columns, row))):
                yield RowBatch(database.source_columns, [row])

    for module in (attempt, api, finalize, keyed_pipeline, adaptive_input, transfer_retry):
        monkeypatch.setattr(module, "get_sql_connection", connect)
    monkeypatch.setattr(attempt, "inspect_source_query_schema", inspect)
    for module in (attempt, adaptive_input, keyed_pipeline):
        monkeypatch.setattr(module, "iter_source_batches", read)
    return database
