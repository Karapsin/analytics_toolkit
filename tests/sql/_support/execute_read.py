"""Small read-oriented DBAPI and ClickHouse drivers for offline I/O tests."""

from __future__ import annotations

import pandas as pd


class FakeClickHouseClient:
    def __init__(self, result: pd.DataFrame | None = None) -> None:
        self.commands: list[str] = []
        self.read_queries: list[str] = []
        self.close_calls = 0
        self.result = result if result is not None else pd.DataFrame({"value": [1]})

    def command(self, sql: str) -> None:
        self.commands.append(sql)

    def query(self, sql: str, *, column_oriented: bool) -> object:
        assert column_oriented is True
        self.read_queries.append(sql)
        return type(
            "QueryResult",
            (),
            {
                "column_names": tuple(str(column) for column in self.result.columns),
                "result_columns": tuple(
                    self.result.iloc[:, index].tolist() for index in range(len(self.result.columns))
                ),
                "row_count": len(self.result),
            },
        )()

    def close(self) -> None:
        self.close_calls += 1


class FakeCursor:
    def __init__(self, connection: FakeDbapiConnection) -> None:
        self.connection = connection
        self.description: list[tuple[str, ...]] | None = None
        self.close_calls = 0

    def execute(self, sql: str) -> None:
        self.connection.executed.append(sql)
        if sql.lower().startswith("select"):
            self.description = [("id",), ("label",)]
        else:
            self.description = None

    def fetchall(self) -> list[tuple[int, str]]:
        return [(1, "ok")]

    def close(self) -> None:
        self.close_calls += 1


class FakeDbapiConnection:
    def __init__(self, name: str = "conn") -> None:
        self.name = name
        self.executed: list[str] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0
        self.cursor_obj = FakeCursor(self)

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1
