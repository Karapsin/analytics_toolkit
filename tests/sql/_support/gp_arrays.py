"""Real psycopg SQL adaptation with an in-memory execute_values cursor boundary."""

from __future__ import annotations

from typing import Any

from psycopg2.extensions import adapt


class AdaptedCursor:
    def __init__(self) -> None:
        self.connection = type("Driver", (), {"encoding": "UTF8"})()
        self.statements: list[bytes] = []
        self.closed = False

    def mogrify(self, template: bytes | str, values: Any) -> bytes:
        if isinstance(template, str):
            template = template.encode("utf-8")
        return template % tuple(adapt(value).getquoted() for value in values)

    def execute(self, statement: bytes) -> None:
        self.statements.append(statement)

    def close(self) -> None:
        self.closed = True


class AdaptedConnection:
    def __init__(self) -> None:
        self.target = AdaptedCursor()
        self.committed = False

    def cursor(self) -> AdaptedCursor:
        return self.target

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.committed = False
