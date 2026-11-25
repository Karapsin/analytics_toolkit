"""Actual GP CREATE receipts and guarded cleanup over native catalog rows."""

from __future__ import annotations

from analytics_toolkit.sql.ddl.api import _create_sql_table_with_connection
from analytics_toolkit.sql.dml.transfer.flow.stage_ownership import stage_creation_callbacks
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.query_native_assignment import (
    QueryNativeConnection,
    QueryNativeDatabase,
)


class NativeStageDatabase(QueryNativeDatabase):
    def __init__(self):
        super().__init__([(7,)], native_type="BIGINT")
        self.drop_failures = {}
        self.drop_attempts = []

    def execute(self, connection, statement, params=None):
        body = strip_query_label(statement)
        if body.lstrip().upper().startswith("DROP "):
            tree = parse_one(body, read="postgres")
            assert isinstance(tree, exp.Drop)
            tables = list(tree.find_all(exp.Table))
            assert len(tables) == 1
            identity = self.identity(tables[0])
            self.drop_attempts.append(identity)
            if identity in self.drop_failures:
                self.commands.append(statement)
                raise self.drop_failures[identity]
        return super().execute(connection, statement, params)

    def identity_from_name(self, name):
        table = parse_one(f"SELECT * FROM {name}", read="postgres").args["from_"].this
        return self.identity(table)


def native_stage_target():
    database = NativeStageDatabase()
    connection = QueryNativeConnection(database, "gp")
    database.opened.append(connection)
    return database, connection


def create_native_owned_stage(
    connection, state, table, *, logical_stage="sandbox.stage", column_types=None
):
    callbacks = stage_creation_callbacks(state, "gp")
    callbacks["on_stage_candidate"](logical_stage)
    _create_sql_table_with_connection(
        "gp",
        connection,
        table,
        connection_key="gp",
        table_schema={"payload": "BIGINT"} if column_types is None else column_types,
        on_resource_created=callbacks["on_resource_created"],
        on_scoped_resource_created=callbacks["on_scoped_resource_created"],
    )
