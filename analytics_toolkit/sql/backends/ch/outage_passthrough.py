"""Preserve native SQL on aliases that did not opt into automatic routing."""

from __future__ import annotations

from typing import Any, cast

import sqlparse
from sqlglot import exp
from sqlglot.errors import SqlglotError

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError

from .metadata import normalize_clickhouse_engine_arg
from .outage_session import configured_cluster
from .outage_statements import exchange_or_rename
from .routing import _LOCAL_SQL_EXECUTION, _RoutingClickHouse

_MODELED = {
    "SELECT",
    "WITH",
    "INSERT",
    "CREATE",
    "DROP",
    "ALTER",
    "DETACH",
    "DELETE",
    "UPDATE",
    "TRUNCATE",
    "EXCHANGE",
    "RENAME",
    "DESCRIBE",
    "DESC",
    "SHOW",
}


def modeled_statement(session: Any, sql: str, parser: Any) -> Any:
    tokens = _native_tokens(sql)
    if _LOCAL_SQL_EXECUTION.get() and [token.normalized for token in tokens[:2]] == [
        "EXISTS",
        "TABLE",
    ]:
        parser(
            "".join(token.value for token in tokens[2:]), into=exp.Table, read=_RoutingClickHouse
        )
        return None
    routed = session.config.cluster_routing is not None
    native = not routed and session.policy == "stop"
    if native and native_command(sql):
        preflight_native(session, sql)
        return None
    special = exchange_or_rename(sql)
    try:
        statement = special[0] if special is not None else parser(sql, read=_RoutingClickHouse)
    except (SqlglotError, RecursionError):
        if not native:
            raise
        preflight_native(session, sql)
        return None
    if isinstance(statement, exp.Command) and special is None:
        if native:
            preflight_native(session, sql)
            return None
        if not routed:
            msg = "Ad hoc availability cannot be verified for this native command."
            raise ClickHouseClusterTopologyError(msg)
    return statement, special


def native_command(sql: str) -> bool:
    first = next(iter(_native_tokens(sql)), None)
    return first is None or first.normalized not in _MODELED


def _native_tokens(sql: str) -> list[Any]:
    return [
        cast("Any", sqlparse.sql.Token)(kind, value)
        for kind, value in cast("Any", sqlparse.lexer.tokenize)(sql)
        if kind not in sqlparse.tokens.Whitespace and kind not in sqlparse.tokens.Comment
    ]


def preflight_native(session: Any, sql: str) -> None:
    clusters = []
    tokens = _native_tokens(sql)
    for index, token in enumerate(tokens[:-2]):
        if token.normalized == "ON" and tokens[index + 1].normalized == "CLUSTER":
            value = tokens[index + 2]
            if (
                value.ttype not in sqlparse.tokens.Name
                and value.ttype not in sqlparse.tokens.Literal.String
            ):
                msg = "Cannot verify the native command's cluster scope."
                raise ClickHouseClusterTopologyError(msg)
            clusters.append(normalize_clickhouse_engine_arg(value.value))
    first = next(iter(tokens), None)
    configured = (
        configured_cluster(session.config)
        if first is not None and first.normalized in _MODELED - {"SHOW", "DESC", "DESCRIBE"}
        else None
    )
    if configured is not None:
        clusters.append(configured)
    for cluster in set(clusters):
        scope = session.scope(cluster)
        scope.require_all(scope.reachable, "Native command preflight")
