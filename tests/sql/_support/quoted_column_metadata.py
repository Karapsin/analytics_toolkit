"""DBAPI metadata boundary for real Explorer completion interactions."""

from __future__ import annotations

from threading import Event
from types import SimpleNamespace

import sqlglot
from analytics_toolkit.sql.execution.cancellation import current_cancellation_scope


class ColumnMetadataProbe:
    def __init__(self):
        self.opened = []
        self.queries = []
        self.markers = []
        self.closed = []
        self.fetched = Event()

    def connect(self, connection_key):
        self.opened.append(connection_key)
        cursor = SimpleNamespace(
            description=[(name,) for name in ("where", "wherever", "label", "id", "warm_only")],
            execute=self._execute,
            fetchall=list,
            close=lambda: self.closed.append("cursor"),
        )
        return SimpleNamespace(
            cursor=lambda: cursor, close=lambda: self.closed.append("connection")
        )

    def _execute(self, statement):
        scope = current_cancellation_scope()
        assert scope is not None
        self.markers.append(scope.marker)
        self.queries.append(statement)
        self.fetched.set()

    def snapshot(self):
        return {
            "opened": list(self.opened),
            "queries": list(self.queries),
            "markers": list(self.markers),
            "closed": list(self.closed),
        }

    def assert_finished(self):
        assert self.opened == ["gp"]
        assert len(self.queries) == 1
        statement = sqlglot.parse_one(self.queries[0], read="postgres")
        assert statement.sql(dialect="postgres", comments=False) == (
            'SELECT * FROM "public"."t" LIMIT 1'
        )
        assert [
            comment.strip() for node in statement.walk() for comment in node.comments or ()
        ] == [f"analytics_toolkit {self.markers[0]}"]
        assert self.closed == ["cursor", "connection"]
