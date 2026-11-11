"""Limit verified coordinator-local upsert execution to its partition ALTER."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlglot import exp, parse_one

from .dialect import ClickHouseDialect

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class _LocalUpsert:
    connection: object
    target: tuple[str, str, str]


_VERIFIED_LOCAL_UPSERT: ContextVar[_LocalUpsert | None] = ContextVar(
    "analytics_toolkit_verified_local_partition_upsert", default=None
)


def _target(table: exp.Expr) -> tuple[str, str, str] | None:
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        return None
    return table.catalog, table.db, table.name


@contextmanager
def verified_local_upsert(connection: object, table_name: str) -> Iterator[None]:
    """Bind storage already verified as coordinator-local by existing-layout inspection.

    This grants no general local-SQL exception: the consumer additionally checks
    the connection, exact identifier components and a partition-drop-only ALTER.
    """
    target = _target(parse_one(table_name, read=ClickHouseDialect, into=exp.Table))
    if target is None:
        message = "Verified local upsert requires a static physical table identifier."
        raise ValueError(message)
    token = _VERIFIED_LOCAL_UPSERT.set(_LocalUpsert(connection, target))
    try:
        yield
    finally:
        _VERIFIED_LOCAL_UPSERT.reset(token)


def verified_local_partition_alter(connection: object, statement: exp.Expr) -> bool:
    context = _VERIFIED_LOCAL_UPSERT.get()
    if (
        context is None
        or context.connection is not connection
        or not isinstance(statement, exp.Alter)
        or statement.args.get("kind") != "TABLE"
        or statement.args.get("cluster") is not None
        or _target(statement.this) != context.target
    ):
        return False
    actions = statement.args.get("actions")
    if not actions:
        return False
    return all(isinstance(action, exp.DropPartition) for action in actions)
