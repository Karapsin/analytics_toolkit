"""Limit verified coordinator-local upsert execution to its partition ALTER."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from analytics_toolkit.sql.execution.upsert_replay import current_upsert_replay

from .coordinator_database import _coordinator_identity
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


def _partition_drop(statement: exp.Expr) -> bool:
    return (
        isinstance(statement, exp.Alter)
        and statement.args.get("kind") == "TABLE"
        and bool(statement.args.get("actions"))
        and all(isinstance(action, exp.DropPartition) for action in statement.args["actions"])
    )


def _canonical(statement: exp.Expr) -> str:
    statement = statement.copy()
    for node in statement.walk():
        node.comments = None
        if isinstance(node, exp.Identifier):
            node.set("quoted", value=False)
    return statement.sql(dialect=ClickHouseDialect)


def certified_partition_rebuild_alter(connection: object, statement: exp.Expr) -> bool:
    """Authorize only the exact partition drops of this connection's frozen payload."""
    context = current_upsert_replay()
    if (
        context is None
        or context.backend != "ch"
        or not _partition_drop(statement)
        or _coordinator_identity(context.connection) != _coordinator_identity(connection)
    ):
        return False
    allowed = context.backend_cache.get("ch_partition_drop_sqls")
    if not isinstance(allowed, frozenset):
        statements = []
        try:
            for sql in context.replay_sqls:
                parsed = ClickHouseDialect().parse(sql)
                if len(parsed) != 1 or parsed[0] is None:
                    return False
                if _partition_drop(parsed[0]):
                    statements.append(_canonical(parsed[0]))
        except SqlglotError:
            return False
        allowed = frozenset(statements)
        context.backend_cache["ch_partition_drop_sqls"] = allowed
    return _canonical(statement) in allowed
