from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import sqlparse
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label

LOCAL = "shard5"
TARGET = "sandbox.events"
PHYSICAL = "sandbox.events_shard"
STAGE = "stage.0123456789abcdef__aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa__w00000"
STAGED_ROWS = 700_004


@dataclass(frozen=True)
class Table:
    rows: int
    columns: tuple[str, ...]
    replicated: bool = True


class ReplacementClient:
    """Independent row/schema state on an initiator and remote storage cluster."""

    def __init__(self, *, storage: str = "core", stage_scope: str = LOCAL) -> None:
        self.storage = storage
        self.tables = {
            (storage, PHYSICAL): Table(17, ("old_column",)),
            (storage, TARGET): Table(0, ("old_column",)),
            (stage_scope, STAGE): Table(STAGED_ROWS, ("id", "value")),
        }
        self.engines = {LOCAL: "Ordinary", "core": "Atomic", "facades": "Atomic"}
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[str] = []
        self.loads: list[tuple[tuple[str, str], tuple[str, str]]] = []
        self.swaps: list[tuple[str, str, str]] = []
        self.local_loads: list[str] = []
        self.incomplete_load = False
        self.bad_final_count = False
        self.fail_rollback = False
        self.rollback_pending = False

    def command(self, sql: str, settings: dict[str, Any] | None = None) -> None:
        self.commands.append((sql, settings or {}))
        clean = strip_query_label(sql)
        tokens = [token for token in sqlparse.parse(clean)[0].tokens if not token.is_whitespace]
        operation = tokens[0].value.upper()
        if operation == "CREATE":
            tree = parse_one(clean, read="clickhouse")
            target = tree.this
            if isinstance(target, exp.Schema):
                columns = tuple(column.name for column in target.expressions)
                target = target.this
            else:
                source = self.tables[self.reference(tree.expression)]
                columns = source.columns
            scope = tree.find(exp.OnCluster)
            key = (scope.this.name if scope else LOCAL, self.table_name(target))
            engine = tree.find(exp.EngineProperty).this.name.lower()
            self.tables.setdefault(
                key, Table(0, columns, replicated=engine.startswith(("replicated", "shared")))
            )
        elif operation == "INSERT":
            index = 3 if tokens[2].value.upper() == "FUNCTION" else 2
            target = self.reference(
                parse_one(f"SELECT * FROM {tokens[index].value}", read="clickhouse")
                .args["from_"]
                .this
            )
            select_index = next(
                i for i, token in enumerate(tokens) if token.value.upper() == "SELECT"
            )
            query = parse_one(
                " ".join(token.value for token in tokens[select_index:]), read="clickhouse"
            )
            source = self.reference(query.args["from_"].this)
            self.loads.append((target, source))
            if target[0] == LOCAL:
                self.local_loads.append(target[1])
            rows = self.tables[source].rows - int(self.incomplete_load)
            self.tables[target] = replace(self.tables[target], rows=rows)
        elif operation in {"EXCHANGE", "RENAME"}:
            if self.rollback_pending and self.fail_rollback:
                message = "rollback unavailable"
                raise OSError(message)
            scope = tokens[-1].value.strip("'\"`") if tokens[-3].value.upper() == "ON" else LOCAL
            left = self.table_name(
                parse_one(f"SELECT * FROM {tokens[2].value}", read="clickhouse").args["from_"].this
            )
            right = self.table_name(
                parse_one(f"SELECT * FROM {tokens[4].value}", read="clickhouse").args["from_"].this
            )
            self.swaps.append((scope, left, right))
            if operation == "EXCHANGE":
                self.tables[scope, left], self.tables[scope, right] = (
                    self.tables[scope, right],
                    self.tables[scope, left],
                )
            else:
                self.tables[scope, right] = self.tables.pop((scope, left))
        elif operation == "DROP":
            tree = parse_one(clean, read="clickhouse")
            scope = tree.find(exp.OnCluster)
            table = tree.this or tree.args["tables"][0]
            self.tables.pop((scope.this.name if scope else LOCAL, self.table_name(table)), None)
        else:
            message = f"Unexpected command: {sql}"
            raise AssertionError(message)

    def query(self, sql: str, **_kwargs: Any) -> Any:
        self.queries.append(sql)
        tree = parse_one(strip_query_label(sql), read="clickhouse")
        source = tree.args["from_"].this
        scope, table = self.reference(source)
        if table == "system.databases":
            return SimpleNamespace(result_rows=[(self.engines[scope],)])
        metadata = {
            "system.tables": [],
            "system.clusters": [(1, 1, "ingest1"), (1, 2, "ingest2")],
            "system.one": [("ingest1",), ("ingest2",)],
            "system.processes": [(0,)],
        }
        if table in metadata:
            return SimpleNamespace(result_rows=metadata[table])
        if table == "system.parts":
            state = self.tables[scope, PHYSICAL]
            return SimpleNamespace(result_rows=[(scope, 1, state.rows, 1, 1)])
        rows = self.tables[scope, table].rows
        # Replicated destinations must be read once per shard, whereas private
        # non-replicated stages need their rows from every replica.
        if (
            isinstance(source, exp.Table)
            and isinstance(source.this, exp.Anonymous)
            and source.this.name.lower() == "clusterallreplicas"
            and not table.startswith("stage.")
            and self.tables[scope, table].replicated
        ):
            rows *= 2
        if table == PHYSICAL and self.swaps and self.bad_final_count:
            self.rollback_pending = True
            rows -= 1
        return SimpleNamespace(result_rows=[(rows,)])

    @staticmethod
    def table_name(table: exp.Table) -> str:
        return f"{table.db}.{table.name}" if table.db else table.name

    @classmethod
    def reference(cls, table: exp.Expression) -> tuple[str, str]:
        if isinstance(table, exp.Subquery):
            return cls.reference(table.this.args["from_"].this)
        if isinstance(table, exp.Table) and isinstance(table.this, exp.Anonymous):
            args = table.this.expressions
            return str(args[0].this), f"{args[1].this}.{args[2].this}"
        assert isinstance(table, exp.Table)
        return LOCAL, cls.table_name(table)
