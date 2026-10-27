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
    replica_rows: tuple[int, int] | None = None


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
        self.current_database = "sandbox"
        self.database_engines: dict[tuple[str, str], str] = {}
        self.fingerprint_calls = 0
        self.concurrent_destination_change = False
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[str] = []
        self.loads: list[tuple[tuple[str, str], tuple[str, str]]] = []
        self.swaps: list[tuple[str, str, str]] = []
        self.local_loads: list[str] = []
        self.incomplete_load = False
        self.bad_final_count = False
        self.fail_rollback = False
        self.rollback_pending = False
        self.lag_replica = False
        self.validation_replica = 1
        self.syncs: list[tuple[str, str, tuple[int, int] | None]] = []
        self.fail_sync = False

    def command(self, sql: str, settings: dict[str, Any] | None = None) -> None:
        self.commands.append((sql, settings or {}))
        clean = strip_query_label(sql)
        tokens = [token for token in sqlparse.parse(clean)[0].tokens if not token.is_whitespace]
        operation = tokens[0].value.upper()
        if operation == "CREATE":
            self._create_table(parse_one(clean, read="clickhouse"))
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
            state = self.tables[target]
            replica_rows = (rows, 0) if self.lag_replica and state.replicated else None
            self.tables[target] = replace(state, rows=rows, replica_rows=replica_rows)
        elif operation == "SYSTEM":
            parts = [
                token.value
                for token in sqlparse.parse(clean)[0].flatten()
                if not token.is_whitespace
            ]
            assert [part.upper() for part in parts[:5]] == [
                "SYSTEM",
                "SYNC",
                "REPLICA",
                "ON",
                "CLUSTER",
            ]
            scope = parts[5].strip("'\"`")
            table = self.table_name(
                parse_one(f"SELECT * FROM {''.join(parts[6:])}", read="clickhouse")
                .args["from_"]
                .this
            )
            self._synchronize(scope, table, settings or {})
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
                database = left.rsplit(".", 1)[0]
                engine = self.database_engines.get((scope, database), self.engines[scope])
                assert engine in {"Atomic", "Replicated"}
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

    def _synchronize(self, scope: str, table: str, settings: dict[str, Any]) -> None:
        assert settings["distributed_ddl_output_mode"] == "throw"
        assert settings["distributed_ddl_task_timeout"] > 0
        if self.fail_sync:
            message = "replica synchronization timed out"
            raise TimeoutError(message)
        state = self.tables[scope, table]
        self.syncs.append((scope, table, state.replica_rows))
        if state.replica_rows is not None:
            acknowledged_rows = max(state.replica_rows)
            self.tables[scope, table] = replace(
                state, replica_rows=(acknowledged_rows, acknowledged_rows)
            )

    def _create_table(self, tree: exp.Create) -> None:
        scope = tree.find(exp.OnCluster)
        execution_scope = scope.this.name if scope else LOCAL
        target = tree.this
        if isinstance(target, exp.Schema):
            columns = tuple(column.name for column in target.expressions)
            target = target.this
        else:
            source_scope, source_table = self.reference(tree.expression)
            if source_scope == LOCAL:
                source_scope = execution_scope
            source = self.tables[source_scope, source_table]
            columns = source.columns
        key = (execution_scope, self.table_name(target))
        engine = tree.find(exp.EngineProperty).this.name.lower()
        self.tables.setdefault(
            key, Table(0, columns, replicated=engine.startswith(("replicated", "shared")))
        )

    def query(self, sql: str, **_kwargs: Any) -> Any:
        self.queries.append(sql)
        clean = strip_query_label(sql)
        if clean == "SELECT currentDatabase()":
            return SimpleNamespace(result_rows=[(self.current_database,)])
        if clean.startswith("EXISTS TABLE "):
            table = parse_one(f"SELECT * FROM {clean[len('EXISTS TABLE ') :]}", read="clickhouse")
            key = self.reference(table.args["from_"].this)
            return SimpleNamespace(result_rows=[(int(key in self.tables),)])
        if "getMacro(" in clean:
            return SimpleNamespace(result_rows=[("core",)])
        tree = parse_one(clean, read="clickhouse")
        source = tree.args["from_"].this
        scope, table = self.reference(source)
        if table.startswith("system."):
            return SimpleNamespace(result_rows=self._metadata_rows(tree, scope, table))
        state = self.tables[scope, table]
        rows = (
            state.replica_rows[self.validation_replica]
            if state.replica_rows is not None
            else state.rows
        )
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

    def _metadata_rows(self, tree: exp.Expression, scope: str, table: str) -> list[tuple[Any, ...]]:
        if table == "system.databases":
            database = next(
                item.expression.this for item in tree.find_all(exp.EQ) if item.this.name == "name"
            )
            return [(self.database_engines.get((scope, database), self.engines[scope]),)]
        if table == "system.parts":
            self.fingerprint_calls += 1
            if self.concurrent_destination_change and self.fingerprint_calls == 2:
                state = self.tables[self.storage, PHYSICAL]
                self.tables[self.storage, PHYSICAL] = replace(state, rows=state.rows + 1)
            filters = {item.this.name: item.expression.this for item in tree.find_all(exp.EQ)}
            target = f"{filters['database']}.{filters['table']}"
            state = self.tables.get((scope, target))
            return [(scope, 1, state.rows, 1, 1)] if state else [(scope, 0, 0, 0, 0)]
        if table in {"system.clusters", "system.one"} and tree.find(exp.Count):
            return [(2,)]
        if table == "system.tables" and tree.find(exp.Count):
            relation = next(
                item.expression.this for item in tree.find_all(exp.EQ) if item.this.name == "name"
            )
            exists = any(
                table_scope == scope and table_name.rsplit(".", 1)[-1] == relation
                for table_scope, table_name in self.tables
            )
            return [(2 * int(exists),)]
        return {
            "system.tables": [],
            "system.clusters": [(1, 1, "ingest1"), (1, 2, "ingest2")],
            "system.one": [("ingest1",), ("ingest2",)],
            "system.processes": [(0,)],
        }[table]

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
