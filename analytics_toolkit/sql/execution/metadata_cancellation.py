"""Tag direct metadata statements so cancellation can find their backend queries."""

from __future__ import annotations

from typing import Any

from .cancellation import current_cancellation_scope, raise_if_cancelled
from .labels import apply_query_label


class _MetadataConnection:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def cursor(self, *args: Any, **kwargs: Any) -> _MetadataConnection:
        raise_if_cancelled()
        return _MetadataConnection(self.connection.cursor(*args, **kwargs))

    def _run(self, method: str, statement: str, *args: Any, **kwargs: Any) -> Any:
        raise_if_cancelled()
        result = getattr(self.connection, method)(
            apply_query_label(statement, None), *args, **kwargs
        )
        raise_if_cancelled()
        return result

    def execute(self, statement: str, *args: Any, **kwargs: Any) -> Any:
        return self._run("execute", statement, *args, **kwargs)

    def query(self, statement: str, *args: Any, **kwargs: Any) -> Any:
        return self._run("query", statement, *args, **kwargs)

    def command(self, statement: str, *args: Any, **kwargs: Any) -> Any:
        return self._run("command", statement, *args, **kwargs)


def cancellable_metadata_connection(connection: Any) -> Any:
    return (
        _MetadataConnection(connection) if current_cancellation_scope() is not None else connection
    )
