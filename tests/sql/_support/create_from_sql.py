from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.sql._support.assertions import strip_query_label as _strip_query_label

create_module = importlib.import_module("analytics_toolkit.sql.dml.table.create_table_from_sql")

ch_fast_path_module = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.create_table_from_sql"
)

sql_module = importlib.import_module("analytics_toolkit.sql")


def observe_selected_query_target(monkeypatch: Any, source: Any, target: Any) -> None:
    """Forbid unowned validation connections and require profiling before DDL."""
    adaptive = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.adaptive_input")

    def unexpected_connection(_key: str) -> None:
        message = "Query validation must reuse its already selected target connection."
        raise AssertionError(message)

    monkeypatch.setattr(adaptive, "get_sql_connection", unexpected_connection)
    command = target.command

    def checked_command(sql: str, settings: dict[str, object] | None = None) -> Any:
        if sql.startswith(("CREATE TABLE", "DROP TABLE")):
            assert max((cursor.read_offset for cursor in source.cursors), default=0) == len(
                source.source_rows
            )
        return command(sql, settings=settings)

    monkeypatch.setattr(target, "command", checked_command)


def acknowledged_create(operation: Any) -> Any:
    """Model a successful target CREATE acknowledgement at the domain boundary."""

    def create(*args: Any, **kwargs: Any) -> Any:
        result = operation(*args, **kwargs)
        callback = kwargs.get("on_resource_created")
        if callback is not None:
            callback(str(args[2]))
        return result

    return create


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class FakeDbapiCursor:
    def __init__(self, connection: FakeDbapiConnection) -> None:
        self.connection = connection
        self.description = connection.description
        self.rowcount = -1
        self.close_calls = 0
        self.read_offset = 0

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        if isinstance(sql, bytes):
            sql = sql.decode("utf-8")
        self.connection.executed.append(sql)
        self.connection.executed_params.append(params)
        if sql.startswith("INSERT INTO "):
            self.rowcount = self.connection.insert_rowcount

    def mogrify(self, statement: Any, values: Any = None) -> bytes:
        text = statement.decode("utf-8") if isinstance(statement, bytes) else statement
        for value in values or ():
            text = text.replace("%s", "NULL" if value is None else repr(value), 1)
        return text.encode("utf-8")

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        rows = self.connection.source_rows[self.read_offset : self.read_offset + size]
        self.read_offset += len(rows)
        return rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []

    def close(self) -> None:
        self.close_calls += 1


class FakeDbapiConnection:
    def __init__(
        self,
        description: list[tuple[Any, ...]] | None = None,
        insert_rowcount: int = 0,
    ) -> None:
        self.description = description or []
        self.encoding = "UTF8"
        self.insert_rowcount = insert_rowcount
        self.source_rows = [
            tuple(None for _column in self.description) for _row in range(insert_rowcount)
        ]
        self.executed: list[str] = []
        self.executed_params: list[tuple[Any, ...] | None] = []
        self.cursors: list[FakeDbapiCursor] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self) -> FakeDbapiCursor:
        cursor = FakeDbapiCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class CloseFailDbapiConnection(FakeDbapiConnection):
    def close(self) -> None:
        self.close_calls += 1
        message = "connection close failed"
        raise RuntimeError(message)


class FakeClickHouseClient:
    def __init__(self, insert_rowcount: int = 0) -> None:
        self.insert_rowcount = insert_rowcount
        self.commands: list[str] = []
        self.command_settings: list[dict[str, object] | None] = []
        self.queries: list[str] = []
        self.created_tables: set[str] = set()
        self.close_calls = 0
        self.data_inserts: list[Any] = []
        self.table_ddls: dict[str, str] = {}

    def insert(self, table: str, data: Any, column_names: Any, **_kwargs: Any) -> None:
        self.data_inserts.append((table, list(column_names), list(data)))

    def command(
        self,
        sql: str,
        settings: dict[str, object] | None = None,
    ) -> dict[str, int] | None:
        self.commands.append(sql)
        self.command_settings.append(settings)
        self._track_table_ddl(sql)
        if sql.startswith("INSERT INTO "):
            return {"written_rows": self.insert_rowcount}
        return None

    def query(self, sql: str) -> FakeResult:
        self.queries.append(sql)
        if sql.startswith("SELECT create_table_query FROM clusterAllReplicas("):
            rows = [
                (
                    "CREATE TABLE analytics.events_shard (id UInt64, amount Decimal(12, 2)) "
                    "ENGINE=MergeTree ORDER BY id",
                )
            ]
            saved = next(
                (ddl for name, ddl in self.table_ddls.items() if name.endswith("_shard")), None
            )
            if saved is not None:
                rows = [(saved,)]
        elif sql.startswith("SHOW CREATE TABLE "):
            rows = [(self._show_create_ddl(sql),)]
        elif sql.startswith("SELECT getMacro("):
            rows = [("core",)]
        elif (
            "clusterAllReplicas" in sql and "system, one" in sql
        ) or "FROM system.clusters" in sql:
            rows = [(1,)]
        elif "clusterAllReplicas" in sql and "system, tables" in sql:
            rows = [(self._cluster_table_count(sql),)]
        elif "clusterAllReplicas" in sql and "system, columns" in sql:
            rows = [(sql.count("name = ") or 1,)]
        elif "clusterAllReplicas" in sql:
            rows = [(len(self.created_tables),)]
        elif sql.startswith("EXISTS TABLE "):
            table_name = sql[len("EXISTS TABLE ") :].strip().replace("`", "")
            rows = [(int(table_name in self.created_tables),)]
        else:
            rows = []
        return FakeResult(rows)

    def _show_create_ddl(self, sql: str) -> str:
        table = sql.removeprefix("SHOW CREATE TABLE ").replace("`", "")
        if table in self.table_ddls:
            return self.table_ddls[table]
        columns = "(id UInt64, amount Decimal(12, 2))"
        if table.endswith("_shard"):
            return f"CREATE TABLE {table} {columns} ENGINE = MergeTree ORDER BY id"
        database, relation = table.rsplit(".", 1)
        return (
            f"CREATE TABLE {table} {columns} "
            f"ENGINE = Distributed('core', '{database}', '{relation}_shard', rand())"
        )

    def close(self) -> None:
        self.close_calls += 1

    def _track_table_ddl(self, sql: str) -> None:
        body = _strip_query_label(sql)
        if body.startswith("CREATE TABLE IF NOT EXISTS "):
            table_name = body[len("CREATE TABLE IF NOT EXISTS ") :].split()[0].replace("`", "")
            self.created_tables.add(table_name)
            self.table_ddls.setdefault(
                table_name, "CREATE TABLE " + body[len("CREATE TABLE IF NOT EXISTS ") :]
            )
            return
        if body.startswith("CREATE TABLE "):
            table_name = body[len("CREATE TABLE ") :].split()[0].replace("`", "")
            self.created_tables.add(table_name)
            self.table_ddls[table_name] = body
            return
        if body.startswith("DROP TABLE IF EXISTS "):
            table_name = body[len("DROP TABLE IF EXISTS ") :].split()[0].replace("`", "")
            self.created_tables.discard(table_name.replace("`", ""))

    def _cluster_table_count(self, sql: str) -> int:
        marker = "AND name = '"
        if marker not in sql:
            return len(self.created_tables)
        relation_name = sql.split(marker, 1)[1].split("'", 1)[0]
        return sum(
            1
            for table_name in self.created_tables
            if table_name.rsplit(".", 1)[-1] == relation_name
        )


SOURCE_DESCRIPTION = [
    ("id", 23, None, None, None, None),
    ("amount", 1700, None, None, 12, 2),
]


def _candidate_create_options(**overrides: object) -> Any:
    values: dict[str, object] = {
        "source_key": "gp",
        "source_backend": "gp",
        "target_key": "gp",
        "target_backend": "gp",
        "target_table": "sandbox.target",
        "source_sql": "SELECT id FROM source",
    }
    values.update(overrides)
    return create_module.CreateTableFromSqlOptions(**values)


__all__ = [
    "SOURCE_DESCRIPTION",
    "Any",
    "CloseFailDbapiConnection",
    "FakeClickHouseClient",
    "FakeDbapiConnection",
    "FakeDbapiCursor",
    "FakeResult",
    "Path",
    "SimpleNamespace",
    "_candidate_create_options",
    "_strip_query_label",
    "acknowledged_create",
    "ch_fast_path_module",
    "create_module",
    "importlib",
    "pytest",
    "sql_module",
    "sys",
]
