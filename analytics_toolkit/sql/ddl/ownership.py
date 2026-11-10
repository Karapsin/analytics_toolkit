"""Report acknowledged CREATE ownership before readiness checks."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.backend_options import preflight_sql_statements

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence


@dataclass
class CreationOwnership:
    resources: list[str] = field(default_factory=list)
    acknowledged_creates: list[str] = field(default_factory=list)
    pending_resource: str | None = None
    submitted: bool = False


_CREATION_OWNERSHIP: ContextVar[CreationOwnership | None] = ContextVar(
    "analytics_toolkit_creation_ownership", default=None
)


@contextmanager
def capture_creation_ownership(ownership: CreationOwnership) -> Iterator[None]:
    token = _CREATION_OWNERSHIP.set(ownership)
    try:
        yield
    finally:
        _CREATION_OWNERSHIP.reset(token)


def acknowledge_created_resource(name: str) -> None:
    ownership = _CREATION_OWNERSHIP.get()
    if ownership is not None:
        ownership.submitted = True
        if name not in ownership.resources:
            ownership.resources.append(name)
        ownership.pending_resource = None


def _created_table_header(sql: str, dialect: Any) -> tuple[str | None, bool, bool]:
    """Parse the target header without interpreting backend-specific table bodies."""
    tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(sql)
    words = [token.text.upper() for token in tokens]
    if not words or words[0] != "CREATE" or "TABLE" not in words[:4]:
        return None, False, False
    table_index = words.index("TABLE") + 1
    replace = words[1:3] == ["OR", "REPLACE"]
    exists = words[table_index : table_index + 3] == ["IF", "NOT", "EXISTS"]
    if exists:
        table_index += 3
    if table_index >= len(tokens):
        return None, False, False
    end_index = table_index
    while end_index + 2 < len(tokens) and words[end_index + 1] == ".":
        end_index += 2
    target_sql = sql[tokens[table_index].start : tokens[end_index].end + 1]
    target = sqlglot.parse_one(target_sql, read=dialect, into=exp.Table)
    if not isinstance(target, exp.Table) or not isinstance(target.this, exp.Identifier):
        return None, False, False
    cluster = words[end_index + 1 : end_index + 3] == ["ON", "CLUSTER"]
    return target.sql(dialect=dialect), cluster, not exists and not replace


def execute_owned_create_sqls(
    adapter: Any,
    connection: Any,
    sqls: Sequence[str],
    on_resource_created: Callable[[str], None],
) -> None:
    preflight_sql_statements(connection, sqls)
    clustered: set[str] = set()
    dialect = adapter.sqlglot_dialect
    headers = [_created_table_header(sql, dialect) for sql in sqls]
    if any(name is None for name, _cluster, _plain in headers):
        message = "Owned creation requires a statically named CREATE TABLE statement."
        raise InvalidSqlInputError(message)
    for sql, (name, has_cluster, plain_create) in zip(sqls, headers):
        if name is not None and has_cluster:
            clustered.add(name)
        context = adapter.local_creation_context(
            connection, local=name in clustered and not has_cluster
        )
        ownership = (
            _CREATION_OWNERSHIP.get()
            if on_resource_created is acknowledge_created_resource
            else None
        )
        if ownership is not None and name is not None:
            ownership.submitted = True
            ownership.pending_resource = name
        with context:
            adapter.execute_command(connection, sql)
        if ownership is not None and name is not None:
            ownership.pending_resource = None
            ownership.acknowledged_creates.append(name)
        if plain_create and name is not None:
            on_resource_created(name)


__all__ = ["execute_owned_create_sqls"]
