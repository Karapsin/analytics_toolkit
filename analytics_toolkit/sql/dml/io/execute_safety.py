from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Sequence

from analytics_toolkit.sql.connection.errors import SqlOperationContext, SqlUtilsError

SqlBatchItemStatus = Literal["success", "failed", "ambiguous", "cancelled"]


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


__all__ = [
    "AmbiguousSqlMutationError",
    "SqlBatchExecutionError",
    "SqlBatchItemResult",
    "SqlBatchItemStatus",
]
