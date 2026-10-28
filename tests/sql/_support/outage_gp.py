"""A dataframe-backed Greenplum transport for public cross-backend transfer tests."""

from __future__ import annotations

from typing import Any

import pandas as pd
import sqlparse
from analytics_toolkit.sql.backends import get_backend_adapter
from sqlglot import ErrorLevel, exp, parse_one


class MemoryGreenplum:
    def __init__(self) -> None:
        self.tables = {
            "db.source": pd.DataFrame({"id": [10, 11]}),
            "db.target": pd.DataFrame({"id": [99]}),
        }
        self.commands: list[str] = []
        self.closed = 0

    def install(self, monkeypatch: Any) -> None:
        adapter = get_backend_adapter("gp")
        monkeypatch.setattr(adapter, "open_connection", lambda *args, **kwargs: Connection(self))
        monkeypatch.setattr(
            adapter,
            "table_exists",
            lambda connection, table_name, **kwargs: table_name.replace('"', "") in self.tables,
        )
        monkeypatch.setattr(
            adapter,
            "get_table_column_types",
            lambda connection, table_name, **kwargs: dict.fromkeys(
                self.tables[table_name.replace('"', "")].columns, "bigint"
            ),
        )
        monkeypatch.setattr(
            adapter,
            "count_table_rows",
            lambda connection, table_name, **kwargs: len(self.tables[table_name.replace('"', "")]),
        )
        monkeypatch.setattr(adapter, "insert_rows_batch", self.insert_rows)
        monkeypatch.setattr(adapter, "insert_dataframe_batch", self.insert_frame)

    def insert_rows(
        self, connection: Any, table: str, columns: Any, rows: Any, **kwargs: Any
    ) -> None:
        table = table.replace('"', "")
        self.tables[table] = pd.concat(
            [self.tables[table], pd.DataFrame(rows, columns=columns)], ignore_index=True
        )
        if kwargs.get("on_progress"):
            kwargs["on_progress"](len(rows))

    def insert_frame(self, connection: Any, table: str, frame: Any, **kwargs: Any) -> None:
        self.insert_rows(
            connection,
            table,
            frame.columns,
            list(frame.itertuples(index=False, name=None)),
            **kwargs,
        )

    def select(self, statement: Any) -> pd.DataFrame:
        tables = [
            item for item in statement.find_all(exp.Table) if isinstance(item.this, exp.Identifier)
        ]
        if not tables or tables[0].db in {"information_schema", "pg_catalog"}:
            return pd.DataFrame()
        relation = tables[0].sql(dialect="postgres").replace('"', "")
        frame = self.tables[relation].copy()
        group = statement.args.get("group")
        if group is not None:
            keys = [item.name for item in group.expressions]
            return frame.groupby(keys, dropna=False).size().reset_index(name="count")
        if statement.find(exp.Count):
            return pd.DataFrame({"count": [len(frame)]})
        selections = statement.expressions
        if selections and all(isinstance(item, exp.Column) for item in selections):
            frame = frame[[item.name for item in selections]]
        limit = statement.args.get("limit")
        if limit is not None and isinstance(limit.expression, exp.Literal):
            frame = frame.head(int(limit.expression.this))
        return frame


class Connection:
    def __init__(self, store: MemoryGreenplum) -> None:
        self.store = store

    def cursor(self, *args: Any, **kwargs: Any) -> Cursor:
        return Cursor(self.store)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.store.closed += 1


class Cursor:
    def __init__(self, store: MemoryGreenplum) -> None:
        self.store = store
        self.frame = pd.DataFrame()
        self.description: list[Any] = []

    def execute(self, sql: str, *args: Any) -> None:
        self.store.commands.append(sql)
        parsed = sqlparse.parse(sql)[0]
        if parsed.get_type() == "CREATE":
            tokens = []
            for token in parsed.tokens:
                if token.normalized in {"WITH", "DISTRIBUTED"}:
                    break
                tokens.append(token.value)
            sql = "".join(tokens)
        statement = parse_one(sql, read="postgres", error_level=ErrorLevel.IGNORE)
        if isinstance(statement, exp.Create):
            table = (
                statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
            )
            name = table.sql(dialect="postgres").replace('"', "")
            columns = [
                item.name for item in statement.this.expressions if isinstance(item, exp.ColumnDef)
            ]
            self.store.tables.setdefault(name, pd.DataFrame(columns=columns))
        elif isinstance(statement, exp.Insert):
            table = (
                statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
            )
            name = table.sql(dialect="postgres").replace('"', "")
            frame = self.store.select(statement.expression)
            columns = list(self.store.tables[name].columns)
            self.store.tables[name] = pd.concat(
                [self.store.tables[name], frame[columns]], ignore_index=True
            )
        elif isinstance(statement, exp.Drop):
            table = statement.this or statement.args["tables"][0]
            self.store.tables.pop(table.sql(dialect="postgres").replace('"', ""), None)
        elif isinstance(statement, exp.Select):
            self.frame = self.store.select(statement)
        self.description = [(name, 20, None, None, None, None) for name in self.frame.columns]

    def fetchmany(self, size: int) -> list[Any]:
        batch = self.frame.head(size)
        self.frame = self.frame.iloc[len(batch) :]
        return list(batch.itertuples(index=False, name=None))

    def fetchall(self) -> list[Any]:
        return self.fetchmany(len(self.frame))

    def fetchone(self) -> Any:
        rows = self.fetchmany(1)
        return rows[0] if rows else None

    def close(self) -> None:
        pass
