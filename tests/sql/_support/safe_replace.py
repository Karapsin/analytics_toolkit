from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from sqlglot import exp, parse_one


class ReplacementCursor:
    def __init__(self, connection: TransactionalReplacementConnection) -> None:
        self.connection = connection

    def execute(self, sql: str) -> None:
        self.connection.executed.append(sql)
        if self.connection.fail_execute_at == len(self.connection.executed):
            message = "cutover failed"
            raise OSError(message)

    def close(self) -> None:
        return


class TransactionalReplacementConnection:
    def __init__(
        self,
        *,
        fail_commit: bool = False,
        fail_execute_at: int | None = None,
    ) -> None:
        self.executed: list[str] = []
        self.fail_commit = fail_commit
        self.commit_calls = 0
        self.rollback_calls = 0
        self.fail_execute_at = fail_execute_at

    def cursor(self) -> ReplacementCursor:
        return ReplacementCursor(self)

    def commit(self) -> None:
        self.commit_calls += 1
        if self.fail_commit:
            message = "connection lost during commit"
            raise OSError(message)

    def rollback(self) -> None:
        self.rollback_calls += 1


def _filters(sql: str) -> dict[str, str]:
    parsed = parse_one(sql, read=ClickHouseDialect)
    return {
        condition.this.name: condition.expression.this
        for condition in parsed.find_all(exp.EQ)
        if isinstance(condition.this, exp.Column) and isinstance(condition.expression, exp.Literal)
    }


class ClickHouseReplacementConnection:
    def __init__(self, *, recovery_artifacts: bool = True, pair: bool = True) -> None:
        self.queries: list[str] = []
        self.commands: list[str] = []
        self.query_settings: list[dict[str, Any] | None] = []
        self.recovery_artifacts = recovery_artifacts
        self.pair = pair
        self.members = ("h1", "h2") if pair else ("h1",)
        columns = "(id Int64, new_column String)"
        self.definitions = {
            "sandbox.target": (
                f"CREATE TABLE sandbox.target {columns} "
                "ENGINE = Distributed('core', 'sandbox', 'target_shard', rand())"
                if pair
                else f"CREATE TABLE sandbox.target {columns} ENGINE = MergeTree ORDER BY id"
            )
        }
        if pair:
            self.definitions["sandbox.target_shard"] = (
                f"CREATE TABLE sandbox.target_shard {columns} ENGINE = MergeTree ORDER BY id"
            )

    def _scope_metadata(self, sql: str) -> list[tuple[Any, ...]] | None:
        if sql.startswith("SELECT getMacro("):
            return [("core",)]
        if sql == "SELECT serverUUID(), tcpPort()":
            return [(str(UUID(int=1)), 9000)]
        if "system.clusters" in sql:
            return [
                (position, 1, host, f"10.0.0.{position}", 9000, int(position == 1))
                if "is_local" in sql
                else (position, 1, 0)
                for position, host in enumerate(self.members, 1)
            ]
        if "system, one" in sql and "serverUUID()" in sql:
            return [
                (position, str(UUID(int=position)), 9000)
                for position, _host in enumerate(self.members, 1)
            ]
        if "system, clusters" in sql:
            return [
                (position, "core", host, f"10.0.0.{target}", 9000)
                for position, _member in enumerate(self.members, 1)
                for target, host in enumerate(self.members, 1)
            ]
        return None

    def _table_metadata(self, sql: str) -> list[tuple[Any, ...]] | None:
        if sql.startswith("SHOW CREATE TABLE "):
            identifier = TableIdentifier.parse(sql[len("SHOW CREATE TABLE ") :], "ch")
            return [(self.definitions[".".join(identifier.parts)],)]
        if "create_table_query" not in sql:
            return None
        filters = _filters(sql)
        definition = self.definitions.get(f"{filters['database']}.{filters['name']}")
        if definition is None:
            return []
        return [
            (position, definition) if "_shard_num" in sql else (definition,)
            for position, _host in enumerate(self.members, 1)
        ]

    def _count(self, sql: str) -> int:
        filters = _filters(sql)
        if "name" in filters:
            table = f"{filters['database']}.{filters['name']}"
            return len(self.members) if table in self.definitions else 0
        return len(self.members)

    def query(self, sql: str, *, settings: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        self.queries.append(sql)
        self.query_settings.append(settings)
        rows = self._scope_metadata(sql)
        if rows is None:
            rows = self._table_metadata(sql)
        if rows is not None:
            return SimpleNamespace(result_rows=rows)
        if "startsWith" in sql and not self.recovery_artifacts:
            return SimpleNamespace(result_rows=[])
        if "system.processes" in sql or "system, processes" in sql:
            return SimpleNamespace(result_rows=[(0,)])
        if "system.parts" in sql or "system, parts" in sql:
            rows = (
                [(host, 1, 1, 1, 1) for host in self.members]
                if "hostName()" in sql
                else [(1, 2, 1, 1)]
            )
            return SimpleNamespace(result_rows=rows)
        if sql.startswith("SELECT count()"):
            return SimpleNamespace(result_rows=[(self._count(sql),)])
        return SimpleNamespace(result_rows=[("target__replace_abcd",)])

    def command(self, sql: str, **_kwargs: Any) -> None:
        self.commands.append(sql)
        if sql.startswith("CREATE TABLE "):
            create = parse_one(sql, read=ClickHouseDialect)
            table = create.this.this if isinstance(create.this, exp.Schema) else create.this
            identifier = TableIdentifier.parse(table.sql(dialect=ClickHouseDialect), "ch")
            self.definitions[".".join(identifier.parts)] = sql
