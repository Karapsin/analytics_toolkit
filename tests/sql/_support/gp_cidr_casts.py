"""CIDR native storage over the unchanged psycopg2 execute_values boundary."""

from __future__ import annotations

import ipaddress
from copy import deepcopy

from sqlglot import exp, parse_one

from tests.sql._support.gp_inet_casts import native_inet_target
from tests.sql._support.gp_temporal_values import TemporalConnection
from tests.sql._support.load_table import load_df_module

SHORT_DOMAIN = '"fixture"."short_text"'


class AssignmentConnection(TemporalConnection):
    def commit(self):
        database = self.database
        tree = parse_one(database._ddl_schema_prefix(database.commands[-1]), read="postgres")
        target_insert = isinstance(tree, exp.Insert) and database.identity(tree.this) == (
            "sandbox",
            "events",
        )
        super().commit()
        if target_insert and database.lost_target_acknowledgements:
            database.lost_target_acknowledgements -= 1
            message = "Lost acknowledgement after committing native target assignment."
            raise ConnectionError(message)


def _decode_domain_assignment(node, casts, *, truncate):
    value = node.this
    # A domain over VARCHAR(3) rejects oversize assignment, while an
    # explicit cast may truncate first. A CAST probe is not a proof.
    if truncate or any(
        cast in {SHORT_DOMAIN, "fixture.short_text", "VARCHAR(3)"} for cast in casts
    ):
        value = value[:3]
    if len(value) > 3:
        message = "Native domain assignment exceeds character varying(3)."
        raise ValueError(message)
    return value


def _decode_assignment(node, native_type, *, truncate_domain=False):
    casts = []
    while isinstance(node, exp.Cast):
        casts.append(node.args["to"].sql(dialect="postgres"))
        node = node.this
    if isinstance(node, exp.Null):
        return None
    if native_type.endswith("[]"):
        if isinstance(node, exp.Literal):
            assert node.this in {"{}", "{NULL}"}
            return [] if node.this == "{}" else [None]
        assert isinstance(node, exp.Array)
        explicit_domain_array = any(
            cast in {SHORT_DOMAIN + "[]", "fixture.short_text[]", "VARCHAR(3)[]"} for cast in casts
        )
        return [
            _decode_assignment(
                item, native_type[:-2], truncate_domain=truncate_domain or explicit_domain_array
            )
            for item in node.expressions
        ]
    assert isinstance(node, exp.Literal)
    if native_type == "INTEGER":
        return int(node.this)
    assert node.is_string
    if native_type in {SHORT_DOMAIN, "fixture.short_text"}:
        return _decode_domain_assignment(node, casts, truncate=truncate_domain)
    assert native_type.upper() in {"CIDR", "INET"}
    # These full-form CIDR fixtures require all host bits to be clear.
    return (
        ipaddress.ip_interface(node.this)
        if native_type.upper() == "INET"
        else ipaddress.ip_network(node.this, strict=True)
    )


def _assign_native_value(value, native_type):
    if value is None:
        return None
    if native_type.endswith("[]"):
        return [_assign_native_value(item, native_type[:-2]) for item in value]
    literal = (
        exp.Literal.number(str(value)) if isinstance(value, int) else exp.Literal.string(str(value))
    )
    return _decode_assignment(literal, native_type)


def _install_catalog(monkeypatch, database):
    original = database.metadata
    domain_oid, domain_array_oid = 91_000, 91_001
    catalog = {
        650: (0, 0, "cidr", "pg_catalog.cidr"),
        651: (0, 650, "cidr[]", "pg_catalog._cidr"),
        1043: (0, 0, "character varying", "pg_catalog.varchar"),
        domain_oid: (1043, 0, SHORT_DOMAIN, "fixture.short_text"),
        domain_array_oid: (0, domain_oid, SHORT_DOMAIN + "[]", "fixture._short_text"),
    }

    def metadata(statement, params):
        if "SELECT a.attname, a.atttypid, a.attndims " in statement:
            table = database.tables[tuple(params[:2])]
            return [
                (name, domain_array_oid if table.schema[name] == SHORT_DOMAIN + "[]" else 651, 1)
                for name in params[2]
            ]
        if "FROM pg_catalog.pg_type AS t " in statement:
            return [catalog[params[0]]]
        return original(statement, params)

    monkeypatch.setattr(database, "metadata", metadata)


def _install_native_select_assignment(monkeypatch, database):
    original = database._insert
    database.assignment_selects = []
    database.private_assignment_attempts = 0
    database.fail_private_assignments = 0

    def insert(tree, identity, params):
        if isinstance(tree.expression, exp.Values):
            return original(tree, identity, params)
        assert isinstance(tree.expression, exp.Select)
        assert tree.expression.find(exp.Cast) is None, (
            identity,
            database.private_assignment_attempts,
            {table: state.schema for table, state in database.tables.items()},
        )
        database.assignment_selects.append(tree.expression)
        if identity != ("sandbox", "events"):
            database.private_assignment_attempts += 1
            if database.fail_private_assignments:
                database.fail_private_assignments -= 1
                message = "Transient failure before final typed-stage assignment."
                raise ConnectionError(message)
        names = [column.name for column in tree.this.expressions] or list(
            database.tables[identity].schema
        )
        rows, _columns = database.select(tree.expression)
        candidate = deepcopy(database.tables[identity])
        candidate.insert(
            names,
            [
                tuple(
                    _assign_native_value(value, candidate.schema[name])
                    for value, name in zip(row, names)
                )
                for row in rows
            ],
        )
        database.tables[identity] = candidate
        return None

    monkeypatch.setattr(database, "_insert", insert)


def native_cidr_target(monkeypatch, native_type="CIDR", *, exists=True, empty=False):
    database = native_inet_target(monkeypatch, native_type)
    seed = (
        7
        if native_type == "INTEGER"
        else "ok"
        if native_type == SHORT_DOMAIN
        else ["ok"]
        if native_type == SHORT_DOMAIN + "[]"
        else [ipaddress.ip_network("198.51.100.0/24")]
        if native_type.endswith("[]")
        else ipaddress.ip_interface("198.51.100.2/24")
        if native_type == "INET"
        else ipaddress.ip_network("198.51.100.0/24")
    )
    database.target.rows = [] if empty else [{"payload": seed}]
    if not exists:
        database.tables.clear()
    database.lost_target_acknowledgements = 0
    monkeypatch.setattr(database, "decode_value", _decode_assignment)
    _install_catalog(monkeypatch, database)
    _install_native_select_assignment(monkeypatch, database)

    def connect(_key):
        connection = AssignmentConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
