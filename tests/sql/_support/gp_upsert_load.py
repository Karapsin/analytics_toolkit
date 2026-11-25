"""Real native GP keyed load checkpoints over independently modeled SQL rows."""

from __future__ import annotations

import pandas as pd
from analytics_toolkit.sql.dml.transfer.runtime import retry
from sqlglot import exp, parse_one

from tests.sql._support.gp_temporal_values import TemporalConnection
from tests.sql._support.load_table import load_df_module
from tests.sql._support.transfer_native_assignment import NativeTransferDatabase


class UpsertLoadDatabase(NativeTransferDatabase):
    def __init__(self, schema):
        super().__init__(rows=[], target_schema=schema)
        self.tables[("public", "target")] = self.tables.pop(("sandbox", "events"))
        self.inserted_frames = []
        self.target_transactions = []
        self.opened = []

    @property
    def target(self):
        identity = (
            ("public", "target") if ("public", "target") in self.tables else ("sandbox", "events")
        )
        return self.tables[identity]

    def execute(self, connection, statement, params=None):
        self._active_connection = connection
        # Guarded catalog/LOCK handling remains in the real shared fake protocol.
        if statement.lstrip().upper().startswith("DELETE FROM"):
            tree = parse_one(statement, read="postgres")
            identity = self.identity(tree.this)
            if identity == ("public", "target"):
                self.commands.append(statement)
                connection.begin_mutation()
                table = self.tables[identity]
                alias = tree.this.alias_or_name
                using = tree.args.get("using") or []
                assert len(using) == 1
                source = using[0]
                source_rows = self.table(source).rows
                table.rows = [
                    row
                    for row in table.rows
                    if not any(
                        self.matches(
                            tree.args.get("where"),
                            {
                                **row,
                                **{f"{alias}.{key}": value for key, value in row.items()},
                                **{
                                    f"{source.alias_or_name}.{key}": value
                                    for key, value in incoming.items()
                                },
                            },
                        )
                        for incoming in source_rows
                    )
                ]
                connection.target_actions.append("DELETE")
                return [], []
        return super().execute(connection, statement, params)

    def _insert(self, tree, identity, params):
        previous = len(self.tables[identity].rows)
        result = super()._insert(tree, identity, params)
        if identity == ("public", "target"):
            self._active_connection.target_actions.append("INSERT")
        elif isinstance(tree.expression, exp.Values):
            columns = [column.name for column in tree.this.expressions]
            rows = self.tables[identity].rows[previous:]
            self.inserted_frames.append(pd.DataFrame(rows, columns=columns))
        return result


class UpsertLoadConnection(TemporalConnection):
    alias = "gp"

    def __init__(self, database):
        super().__init__(database)
        self.target_actions = []

    def commit(self):
        super().commit()
        if self.target_actions:
            self.database.target_transactions.append(tuple(self.target_actions))
            self.target_actions.clear()

    def rollback(self):
        super().rollback()
        self.target_actions.clear()


def native_upsert_load(monkeypatch, schema):
    database = UpsertLoadDatabase(schema)

    def connect(_alias):
        connection = UpsertLoadConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    monkeypatch.setattr(retry, "get_sql_connection", connect)
    return database
