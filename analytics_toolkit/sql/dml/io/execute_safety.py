from __future__ import annotations

# ruff: noqa: PYI034, PYI036
from dataclasses import dataclass
from typing import Any, Literal, Sequence, cast

import sqlparse

from analytics_toolkit.sql.connection.errors import SqlOperationContext, SqlUtilsError

ExecuteRetryPolicy = Literal["safe", "always", "never"]
SqlBatchItemStatus = Literal["success", "failed", "ambiguous", "cancelled"]

_READ_ONLY_STATEMENT_TYPES = {"SELECT", "SHOW", "DESCRIBE"}


@dataclass(frozen=True)
class SqlBatchItemResult:
    index: int
    query: str
    status: SqlBatchItemStatus
    attempts: int
    result: Any = None
    error: BaseException | None = None


class AmbiguousSqlMutationError(SqlUtilsError):
    """A mutating statement may have reached the database before its connection failed."""

    analytics_toolkit_sql_retry_safe = False

    def __init__(
        self,
        message: str,
        *,
        context: SqlOperationContext,
        original_error: Exception,
    ) -> None:
        super().__init__(message)
        self.context = context
        self.original_error = original_error
        self.sql_context = context


class SqlBatchExecutionError(SqlUtilsError):
    """Structured outcome for an execute batch that did not fully succeed."""

    analytics_toolkit_sql_retry_safe = False

    def __init__(
        self,
        batch_id: str,
        items: Sequence[SqlBatchItemResult],
    ) -> None:
        self.batch_id = batch_id
        self.items = tuple(sorted(items, key=lambda item: item.index))
        super().__init__(
            f"SQL batch {batch_id} did not fully succeed: "
            + ", ".join(f"{item.index}={item.status}" for item in self.items)
        )

    def _indexes(self, *statuses: SqlBatchItemStatus) -> tuple[int, ...]:
        return tuple(item.index for item in self.items if item.status in statuses)

    @property
    def successful_indexes(self) -> tuple[int, ...]:
        return self._indexes("success")

    @property
    def failed_indexes(self) -> tuple[int, ...]:
        return self._indexes("failed")

    @property
    def ambiguous_indexes(self) -> tuple[int, ...]:
        return self._indexes("ambiguous")

    @property
    def cancelled_indexes(self) -> tuple[int, ...]:
        return self._indexes("cancelled")

    @property
    def safe_to_retry_indexes(self) -> tuple[int, ...]:
        return self._indexes("failed", "cancelled")

    @property
    def safe_to_retry_queries(self) -> tuple[str, ...]:
        safe = set(self.safe_to_retry_indexes)
        return tuple(item.query for item in self.items if item.index in safe)


@dataclass
class ExecuteAttemptState:
    submitted: bool = False
    commit_started: bool = False
    committed: bool = False


def validate_execute_retry_policy(value: str) -> ExecuteRetryPolicy:
    if value not in {"safe", "always", "never"}:
        message = "retry_policy must be one of: safe, always, never."
        raise ValueError(message)
    return cast("ExecuteRetryPolicy", value)


def is_read_only_sql(sql: str) -> bool:
    statements = [statement for statement in sqlparse.parse(sql) if str(statement).strip()]
    return bool(statements) and all(
        statement.get_type().upper() in _READ_ONLY_STATEMENT_TYPES  # type: ignore[no-untyped-call]
        for statement in statements
    )


class TrackingConnection:
    """DB-API proxy recording whether a mutation reached execute/commit."""

    def __init__(self, connection: Any, state: ExecuteAttemptState) -> None:
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_state", state)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def cursor(self, *args: Any, **kwargs: Any) -> TrackingCursor:
        return TrackingCursor(self._connection.cursor(*args, **kwargs), self._state)

    def commit(self) -> Any:
        self._state.commit_started = True
        result = self._connection.commit()
        self._state.committed = True
        return result


class TrackingCursor:
    def __init__(self, cursor: Any, state: ExecuteAttemptState) -> None:
        self._cursor = cursor
        self._state = state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __enter__(self) -> TrackingCursor:
        enter = getattr(self._cursor, "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, *args: Any) -> Any:
        exit_method = getattr(self._cursor, "__exit__", None)
        if callable(exit_method):
            return exit_method(*args)
        self._cursor.close()
        return None

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self._state.submitted = True
        return self._cursor.execute(*args, **kwargs)


__all__ = [
    "AmbiguousSqlMutationError",
    "ExecuteAttemptState",
    "ExecuteRetryPolicy",
    "SqlBatchExecutionError",
    "SqlBatchItemResult",
    "SqlBatchItemStatus",
    "TrackingConnection",
    "is_read_only_sql",
    "validate_execute_retry_policy",
]
