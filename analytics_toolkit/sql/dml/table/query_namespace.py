"""Keep a setup-selected target name fixed through creation retries."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends.query_namespace import bind_query_target_namespace
from analytics_toolkit.sql.core.identifiers import TableIdentifier

if TYPE_CHECKING:
    from .models import CreateTableFromSqlOptions


def needs_setup_namespace(options: Any) -> bool:
    if (
        not getattr(options, "setup_sqls", ())
        or getattr(options, "source_key", None) != options.target_key
    ):
        return False
    identity = TableIdentifier.parse(options.target_table, options.target_backend)
    return len(identity.parts) < (3 if options.target_backend == "trino" else 2)


def cached_query_target(options: CreateTableFromSqlOptions) -> CreateTableFromSqlOptions:
    target = getattr(options, "input_resolution", {}).get("bound_query_target")
    return replace(options, target_table=target) if target is not None else options


def bind_script_query_target(
    options: CreateTableFromSqlOptions, connection: Any
) -> CreateTableFromSqlOptions:
    if not needs_setup_namespace(options):
        return cached_query_target(options)
    target = bind_query_target_namespace(options.target_backend, connection, options.target_table)
    options.input_resolution["bound_query_target"] = target
    return replace(options, target_table=target)
