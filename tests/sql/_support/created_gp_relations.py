"""Catalog rows belong to the current native relation object, never its name."""

from __future__ import annotations

import sqlparse
from analytics_toolkit.sql.connection.config import get_connection_config
from sqlglot import exp, parse_one


def locked_relation(statement):
    text = sqlparse.format(statement, strip_comments=True).strip().rstrip(";")
    prefix, suffix = "LOCK TABLE ", " IN ACCESS EXCLUSIVE MODE"
    if not text.upper().startswith(prefix):
        return None
    assert text.upper().endswith(suffix)
    identifier = text[len(prefix) : -len(suffix)]
    table = parse_one(f"SELECT * FROM {identifier}", read="postgres").args["from_"].this
    assert isinstance(table, exp.Table)
    assert table.args.get("alias") is None
    return table


def relation_catalog_result(database, connection, statement, params):
    locked = locked_relation(statement)
    if locked is not None:
        database.commands.append(statement)
        assert database.identity(locked) in database.tables
        database.result_types = []
        return [], []
    if not all(
        fragment in statement
        for fragment in ("pg_catalog.pg_class", "pg_catalog.to_regclass", "c.relfilenode")
    ):
        return None
    database.commands.append(statement)
    assert len(params) == 1
    reference = parse_one(f"SELECT * FROM {params[0]}", read="postgres").args["from_"].this
    identity = database.identity(reference)
    table = database.tables.get(identity)
    database.result_types = ["TEXT", "TEXT", "INTEGER", "BIGINT", "BIGINT", "BIGINT"]
    names = ["database", "server_address", "server_port", "oid", "relfilenode", "namespace"]
    if table is None:
        return [], names
    if not hasattr(table, "native_creation_oid"):
        database.next_relation_oid += 1
        table.native_creation_oid = database.next_relation_oid
    schema = identity[0]
    if schema not in database.namespace_oids:
        database.namespace_oids[schema] = 2000 + len(database.namespace_oids)
    oid = table.native_creation_oid
    config = get_connection_config(connection.alias)
    return [
        (config.database, "127.0.0.1", 5432, oid, oid + 100000, database.namespace_oids[schema])
    ], names
