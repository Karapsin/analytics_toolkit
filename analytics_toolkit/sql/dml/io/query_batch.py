from __future__ import annotations

from typing import Callable, TypeVar, cast
from uuid import uuid4

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.dml.io.execute_sql import (
    _effective_execute_concurrency,
    _execute_sql_batch,
    _normalize_execute_queries,
    _validate_execute_concurrency_options,
)
from analytics_toolkit.sql.dml.io.models import ExecuteSqlOptions

T = TypeVar("T")


def run_query_batch(  # noqa: PLR0913
    *,
    query: str | list[str],
    table_name: str | list[str] | None,
    prepare: Callable[[str, str | None, list[int]], Callable[[], T]],
    concurrency: int,
    soft_concurrency_cap: int | None,
    hard_concurrency_cap: int,
    plan_only: bool = False,
) -> T | list[T]:
    """Validate every item before running independent operations on fresh connections."""
    _validate_execute_concurrency_options(
        concurrency=concurrency,
        soft_concurrency_cap=soft_concurrency_cap,
        hard_concurrency_cap=hard_concurrency_cap,
    )
    queries, is_batch = _normalize_execute_queries(query)
    if isinstance(table_name, list):
        if not is_batch or len(table_name) != len(queries):
            message = "table_name lists require a query list of the same length."
            raise InvalidSqlInputError(message)
        targets: list[str | None] = list(table_name)
    else:
        targets = [table_name] * len(queries)
    workers = (
        _effective_execute_concurrency(
            concurrency=concurrency,
            soft_concurrency_cap=soft_concurrency_cap,
            hard_concurrency_cap=hard_concurrency_cap,
        )
        if is_batch
        else 1
    )
    batch_id = uuid4().hex if is_batch else None
    items = [
        ExecuteSqlOptions(
            connection_key="",
            backend="",
            sql=sql,
            batch_id=batch_id,
            batch_index=index,
        )
        for index, sql in enumerate(queries)
    ]
    operations = [
        prepare(item.sql, target, item.attempt_numbers) for item, target in zip(items, targets)
    ]
    if not is_batch:
        return operations[0]()
    if plan_only:
        return [operation() for operation in operations]
    return _execute_sql_batch(
        items,
        concurrency=workers,
        execute_item=lambda item: operations[cast("int", item.batch_index)](),
    )
