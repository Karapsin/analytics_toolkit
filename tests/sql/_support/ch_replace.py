from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import sqlparse
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_replace_metadata import ReplacementMetadata

LOCAL = "shard5"
TARGET = "sandbox.events"
PHYSICAL = "sandbox.events_shard"
STAGE = "stage.0123456789abcdef__aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa__w00000"
STAGED_ROWS = 700_004


class FixtureTableAlreadyExistsError(RuntimeError):
    code = 57


@dataclass(frozen=True)
class Table:
    rows: int
    columns: tuple[str, ...]
    replicated: bool = True
    replica_rows: tuple[int, ...] | None = None
    uuid: UUID = field(default_factory=uuid4, compare=False)
    create: exp.Create | None = field(default=None, compare=False, repr=False)


class ReplacementClient:
    """Independent row/schema state on an initiator and remote storage cluster."""

    def __init__(
        self,
        *,
        storage: str = "core",
        stage_scope: str | None = None,
        coordinator_scope: str = LOCAL,
    ) -> None:
        self.storage = storage
        self.coordinator_scope = coordinator_scope
        stage_scope = stage_scope or coordinator_scope
        self.scope_aliases = {"{cluster}": storage}
        self.tables = {
            (storage, PHYSICAL): Table(17, ("old_column",)),
            (storage, TARGET): Table(0, ("old_column",)),
            (stage_scope, STAGE): Table(STAGED_ROWS, ("id", "value")),
        }
        self.engines = {LOCAL: "Ordinary", "core": "Atomic", "facades": "Atomic"}
        if coordinator_scope != storage:
            self.engines[coordinator_scope] = "Atomic"
        self.facade_sources = {(storage, TARGET): (storage, PHYSICAL)}
        self.column_types: dict[tuple[str, str], tuple[str, ...]] = {}
        if coordinator_scope != storage:
            self.tables[coordinator_scope, TARGET] = Table(0, ("old_column",))
            self.facade_sources[coordinator_scope, TARGET] = (storage, PHYSICAL)
        self.internal_replication = False
        self.replica_count = 1 if coordinator_scope == storage else 2
        self.current_database = "sandbox"
        self.database_engines: dict[tuple[str, str], str] = {}
        self.fingerprint_calls = 0
        self.concurrent_destination_change = False
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[str] = []
        self.loads: list[tuple[tuple[str, str], tuple[str, str]]] = []
        self.swaps: list[tuple[str, str, str]] = []
        self.cleanup_markers: list[tuple[str, str, str]] = []
        self.local_loads: list[str] = []
        self.incomplete_load = False
        self.bad_final_count = False
        self.fail_rollback = False
        self.rollback_pending = False
        self.lag_replica = False
        self.validation_replica = self.replica_count - 1
        self.syncs: list[tuple[str, str, tuple[int, ...] | None]] = []
        self.fail_sync = False
        self.fail_cutover_at: int | None = None
        self.fail_cutover_after_apply = False
        self.cutover_attempts = 0
        self.metadata = ReplacementMetadata(self)
        self.default_replica_path = "/clickhouse/{database}/{table}/{shard}"

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
            if self._scope(target[0]) == self._scope(self.coordinator_scope):
                self.local_loads.append(target[1])
            rows = self.tables[source].rows - int(self.incomplete_load)
            state = self.tables[target]
            replica_rows = (rows, 0) if self.lag_replica and state.replicated else None
            if not state.replicated:
                striped = tokens[index].value.lower().startswith("clusterallreplicas(")
                if striped:
                    quotient, remainder = divmod(rows, self.replica_count)
                    replica_rows = tuple(
                        quotient + int(index < remainder) for index in range(self.replica_count)
                    )
                else:
                    replica_rows = (rows,) * self.replica_count
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
            scope = self._scope(parts[5].strip("'\"`"))
            table = self.table_name(
                parse_one(f"SELECT * FROM {''.join(parts[6:])}", read="clickhouse")
                .args["from_"]
                .this
            )
            self._synchronize(scope, table, settings or {})
        elif operation in {"EXCHANGE", "RENAME"}:
            self._cutover_statement(operation, tokens)
        elif operation == "DROP":
            tree = parse_one(clean, read="clickhouse")
            scope = tree.find(exp.OnCluster)
            table = tree.this or tree.args["tables"][0]
            key = (
                self._scope(scope.this.name) if scope else self.coordinator_scope,
                self.table_name(table),
            )
            self.tables.pop(key, None)
            self.facade_sources.pop(key, None)
            self.column_types.pop(key, None)
        else:
            message = f"Unexpected command: {sql}"
            raise AssertionError(message)

    def _cutover_statement(self, operation: str, tokens: list[Any]) -> None:
        if self.rollback_pending and self.fail_rollback:
            message = "rollback unavailable"
            raise OSError(message)
        self.cutover_attempts += 1
        failing = self.fail_cutover_at == self.cutover_attempts
        if failing and not self.fail_cutover_after_apply:
            message = "cutover failed before execution"
            raise OSError(message)
        scope = (
            self._scope(tokens[-1].value.strip("'\"`"))
            if tokens[-3].value.upper() == "ON"
            else self.coordinator_scope
        )
        left, right = (
            self.table_name(
                parse_one(f"SELECT * FROM {tokens[index].value}", read="clickhouse")
                .args["from_"]
                .this
            )
            for index in (2, 4)
        )
        operations = self.cleanup_markers if right.endswith("__discardable") else self.swaps
        operations.append((scope, left, right))
        left_source = self.facade_sources.get((scope, left))
        right_source = self.facade_sources.get((scope, right))
        left_types = self.column_types.get((scope, left))
        right_types = self.column_types.get((scope, right))
        if operation == "EXCHANGE":
            database = left.rsplit(".", 1)[0]
            engine = self.database_engines.get((scope, database), self.engines[scope])
            assert engine in {"Atomic", "Replicated"}
            self.tables[scope, left], self.tables[scope, right] = (
                self.tables[scope, right],
                self.tables[scope, left],
            )
            self.column_types.pop((scope, left), None)
            self.column_types.pop((scope, right), None)
            if right_source is not None:
                self.facade_sources[scope, left] = right_source
            if right_types is not None:
                self.column_types[scope, left] = right_types
        else:
            if (scope, right) in self.tables:
                message = "rename destination already exists"
                raise OSError(message)
            self.tables[scope, right] = self.tables.pop((scope, left))
            self.facade_sources.pop((scope, left), None)
            self.column_types.pop((scope, left), None)
        if left_source is not None:
            self.facade_sources[scope, right] = left_source
        if left_types is not None:
            self.column_types[scope, right] = left_types
        if failing:
            message = "cutover acknowledgement lost after execution"
            raise OSError(message)

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
                state, replica_rows=(acknowledged_rows,) * len(state.replica_rows)
            )

    def _create_table(self, tree: exp.Create) -> None:
        scope = tree.find(exp.OnCluster)
        execution_scope = self._scope(scope.this.name) if scope else self.coordinator_scope
        target = tree.this
        if isinstance(target, exp.Schema):
            columns = tuple(column.name for column in target.expressions)
            types = tuple(column.kind.sql(dialect="clickhouse") for column in target.expressions)
            target = target.this
        else:
            source_scope, source_table = self.reference(tree.expression)
            if source_scope == self.coordinator_scope:
                source_scope = execution_scope
            source = self.tables[source_scope, source_table]
            columns = source.columns
            types = self.column_types.get((source_scope, source_table), ("Int64",) * len(columns))
        key = (execution_scope, self.table_name(target))
        if key in self.tables:
            if tree.args.get("exists"):
                return
            message = "The table already exists in the selected fixture scope."
            raise FixtureTableAlreadyExistsError(message)
        engine = tree.find(exp.EngineProperty).this.name.lower()
        if key not in self.tables:
            self.column_types[key] = types
        declared_uuid = tree.find(exp.UuidProperty)
        self.tables.setdefault(
            key,
            Table(
                0,
                columns,
                replicated=engine.startswith(("replicated", "shared")),
                uuid=UUID(declared_uuid.this.name) if declared_uuid is not None else uuid4(),
                create=tree.copy(),
            ),
        )
        if engine == "distributed":
            arguments = tree.find(exp.EngineProperty).this.expressions
            self.facade_sources[key] = (
                self._scope(str(arguments[0].this)),
                f"{arguments[1].this}.{arguments[2].this}",
            )

    def _command_rows(self, clean: str) -> list[tuple[Any, ...]] | None:
        if clean.startswith("SHOW CREATE TABLE "):
            name = clean.removeprefix("SHOW CREATE TABLE ").replace("`", "")
            return [(self.metadata.ddl((self.coordinator_scope, name)),)]
        if clean == "SELECT currentDatabase()":
            return [(self.current_database,)]
        if clean.startswith("EXISTS TABLE "):
            table = parse_one(f"SELECT * FROM {clean[len('EXISTS TABLE ') :]}", read="clickhouse")
            key = self.reference(table.args["from_"].this)
            return [(int(key in self.tables),)]
        if "getMacro(" in clean:
            return [(self.storage,)]
        if clean == "SELECT serverUUID(), tcpPort()":
            return [self.metadata.local_identity()]
        return None

    def query(self, sql: str, **_kwargs: Any) -> Any:
        self.queries.append(sql)
        clean = strip_query_label(sql)
        command_rows = self._command_rows(clean)
        if command_rows is not None:
            return SimpleNamespace(result_rows=command_rows)
        tree = parse_one(clean, read="clickhouse")
        source = tree.args["from_"].this
        scope, table = self.reference(source)
        if table.startswith("system."):
            return SimpleNamespace(result_rows=self._metadata_rows(tree, scope, table))
        scope, table = self.facade_sources.get((scope, table), (scope, table))
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
        ):
            rows = (
                sum(state.replica_rows)
                if state.replica_rows is not None
                else rows * self.replica_count
            )
        if table == PHYSICAL and self.swaps and self.bad_final_count:
            self.rollback_pending = True
            rows -= 1
        return SimpleNamespace(result_rows=[(rows,)])

    def _metadata_rows(self, tree: exp.Expression, scope: str, table: str) -> list[tuple[Any, ...]]:
        proof = self.metadata.rows(tree, scope, table)
        return self._legacy_metadata_rows(tree, scope, table) if proof is None else proof

    def _legacy_metadata_rows(
        self, tree: exp.Expression, scope: str, table: str
    ) -> list[tuple[Any, ...]]:
        catalog_rows = self._catalog_rows(tree, scope, table)
        if catalog_rows is not None:
            return catalog_rows
        if table in {"system.clusters", "system.one"} and tree.find(exp.Count):
            return [(self.replica_count,)]
        if table == "system.clusters" and any(
            item.name == "internal_replication" for item in tree.expressions
        ):
            return [
                (1, replica, self.internal_replication)
                for replica in range(1, self.replica_count + 1)
            ]
        if table == "system.tables" and any(
            item.name == "create_table_query" for item in tree.expressions
        ):
            filters = {item.this.name: item.expression.this for item in tree.find_all(exp.EQ)}
            target = f"{filters['database']}.{filters['name']}"
            state = self.tables[scope, target]
            columns = ", ".join(f"`{column}` Int64" for column in state.columns)
            engine = "ReplicatedMergeTree" if state.replicated else "MergeTree"
            return [(f"CREATE TABLE {target} ({columns}) ENGINE={engine} ORDER BY tuple()",)]
        if table == "system.tables" and tree.find(exp.Count):
            relation = next(
                item.expression.this for item in tree.find_all(exp.EQ) if item.this.name == "name"
            )
            exists = any(
                table_scope == scope and table_name.rsplit(".", 1)[-1] == relation
                for table_scope, table_name in self.tables
            )
            return [(self.replica_count * int(exists),)]
        return {
            "system.tables": self._artifact_rows(tree, scope) if tree.args.get("distinct") else [],
            "system.clusters": [
                (1, replica, f"ingest{replica}") for replica in range(1, self.replica_count + 1)
            ],
            "system.one": [(f"ingest{replica}",) for replica in range(1, self.replica_count + 1)],
            "system.processes": [(0,)],
        }[table]

    def _catalog_rows(
        self, tree: exp.Expression, scope: str, table: str
    ) -> list[tuple[Any, ...]] | None:
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
        return None

    def _artifact_rows(self, tree: exp.Expression, scope: str) -> list[tuple[Any, ...]]:
        database = next(
            item.expression.this for item in tree.find_all(exp.EQ) if item.this.name == "database"
        )
        prefixes = tuple(
            str(item.this)
            for item in tree.find_all(exp.Literal)
            if str(item.this).endswith(("__replace_", "__backup_"))
        )
        return [
            (name.rsplit(".", 1)[-1],)
            for (table_scope, name) in self.tables
            if table_scope == scope
            and name.rsplit(".", 1)[0] == database
            and name.rsplit(".", 1)[-1].startswith(prefixes)
        ]

    @staticmethod
    def table_name(table: exp.Table) -> str:
        return f"{table.db}.{table.name}" if table.db else table.name

    def _scope(self, scope: str) -> str:
        return self.scope_aliases.get(scope, scope)

    def reference(self, table: exp.Expression) -> tuple[str, str]:
        if isinstance(table, exp.Subquery):
            return self.reference(table.this.args["from_"].this)
        if isinstance(table, exp.Table) and isinstance(table.this, exp.Anonymous):
            args = table.this.expressions
            return self._scope(str(args[0].this)), f"{args[1].this}.{args[2].this}"
        assert isinstance(table, exp.Table)
        return self.coordinator_scope, self.table_name(table)
