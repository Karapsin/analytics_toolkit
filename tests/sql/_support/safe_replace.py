from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class ClickHouseReplacementConnection:
    def __init__(self, *, recovery_artifacts: bool = True, pair: bool = True) -> None:
        self.queries: list[str] = []
        self.commands: list[str] = []
        self.recovery_artifacts = recovery_artifacts
        self.pair = pair

    def query(self, sql: str) -> Any:
        self.queries.append(sql)
        columns = "(id Int64, new_column String)"
        physical = f"CREATE TABLE sandbox.target_shard {columns} ENGINE = MergeTree ORDER BY id"
        if sql.startswith("SHOW CREATE TABLE"):
            ddl = (
                f"CREATE TABLE sandbox.target {columns} "
                "ENGINE = Distributed('core', 'sandbox', 'target_shard', rand())"
                if self.pair
                else f"CREATE TABLE sandbox.target {columns} ENGINE = MergeTree ORDER BY id"
            )
            return SimpleNamespace(result_rows=[(ddl,)])
        if "create_table_query" in sql:
            return SimpleNamespace(result_rows=[(physical,)])
        if "startsWith" in sql and not self.recovery_artifacts:
            return SimpleNamespace(result_rows=[])
        if "system.processes" in sql or "system, processes" in sql:
            return SimpleNamespace(result_rows=[(0,)])
        if sql.startswith("SELECT count()"):
            return SimpleNamespace(result_rows=[(1,)])
        return SimpleNamespace(result_rows=[("target__replace_abcd",)])

    def command(self, sql: str, **_kwargs: Any) -> None:
        self.commands.append(sql)
