"""Verified existing storage identity, independent of a desired creation policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlglot import exp

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.ddl.identifiers import quote_identifier

from .ddl import build_ch_shard_table_name
from .metadata import extract_clickhouse_distributed_shard_table
from .reconfigure_ddl import (
    _column_without_ttl,
    engine_name,
    engine_sql,
    parse_create_table,
)
from .reconfigure_support import (
    qualify_with_database,
    resolve_optional_cluster,
    show_create_table,
    show_create_table_on_cluster,
    table_database,
    unquoted_table_name,
)
from .routing import local_sql


@dataclass(frozen=True)
class ExistingClickHouseLayout:
    logical_table: str
    physical_table: str
    create_distributed_pair: bool
    shard_on_cluster: str | None
    distributed_on_cluster: str | None
    distributed_cluster: str | None
    ddl: str
    physical_ddl: str
    owns_physical: bool = True


def _inspect_existing_layout_local(
    connection: Any,
    table: str,
    *,
    shard_on_cluster: str | None = None,
    distributed_on_cluster: str | None = None,
    require_owned: bool = False,
) -> ExistingClickHouseLayout:
    """Own only the named local table or a verified, explicitly referenced pair.

    A suffix is never evidence of ownership. DDL execution scopes are supplied
    separately from the Distributed engine's routing cluster.
    """
    try:
        ddl = show_create_table(connection, table)
    except Exception:
        if distributed_on_cluster is None:
            raise
        cluster = resolve_optional_cluster(connection, distributed_on_cluster)
        if cluster is None:
            raise
        ddl = show_create_table_on_cluster(connection, table, cluster)
    create = parse_create_table(ddl, table)
    if engine_name(create).lower() != "distributed":
        database = unquoted_table_name(table)[0] or table_database(connection, create)
        qualified = qualify_with_database(table, database)
        return ExistingClickHouseLayout(
            logical_table=qualified,
            physical_table=qualified,
            create_distributed_pair=False,
            shard_on_cluster=shard_on_cluster,
            distributed_on_cluster=None,
            distributed_cluster=None,
            ddl=ddl,
            physical_ddl=ddl,
        )
    database = table_database(connection, create)
    reference = extract_clickhouse_distributed_shard_table(engine_sql(create), database)
    qualified = qualify_with_database(table, database)
    if reference is None:
        message = f"Cannot verify ClickHouse Distributed storage for {table}."
        raise InvalidSqlInputError(message)
    physical = (
        f"{quote_identifier(reference.database, 'ch')}.{quote_identifier(reference.table, 'ch')}"
    )
    expected = build_ch_shard_table_name(qualified)
    owns_physical = unquoted_table_name(physical) == unquoted_table_name(expected)
    if require_owned and not owns_physical:
        message = f"ClickHouse table {table} is not a managed Distributed/_shard pair."
        raise InvalidSqlInputError(message)
    cluster = resolve_optional_cluster(connection, reference.cluster)
    if cluster is None:
        message = "Cannot resolve the referenced ClickHouse storage cluster."
        raise InvalidSqlInputError(message)
    physical_ddl = show_create_table_on_cluster(connection, physical, cluster)
    physical_create = parse_create_table(physical_ddl, physical)
    if not _is_direct_storage(physical_create):
        message = f"Cannot verify directly stored ClickHouse data for {physical}."
        raise InvalidSqlInputError(message)
    owns_physical = owns_physical and _column_identity(create) == _column_identity(physical_create)
    if require_owned and not owns_physical:
        message = f"ClickHouse table {table} is not a managed Distributed/_shard pair."
        raise InvalidSqlInputError(message)
    return ExistingClickHouseLayout(
        logical_table=qualified,
        physical_table=physical,
        create_distributed_pair=True,
        shard_on_cluster=shard_on_cluster or reference.cluster,
        distributed_on_cluster=distributed_on_cluster,
        distributed_cluster=reference.cluster,
        ddl=ddl,
        physical_ddl=physical_ddl,
        owns_physical=owns_physical,
    )


def _column_identity(create: exp.Create) -> tuple[str, ...]:
    schema = create.this
    return tuple(
        _logical_column_definition(item)
        for item in schema.expressions
        if isinstance(item, exp.ColumnDef)
    )


def _logical_column_definition(column: exp.ColumnDef) -> str:
    logical = _column_without_ttl(column)
    logical.set(
        "constraints",
        [
            constraint
            for constraint in logical.constraints
            if not isinstance(constraint.kind, exp.CompressColumnConstraint)
        ],
    )
    return logical.sql(dialect="clickhouse")


def _is_direct_storage(create: exp.Create) -> bool:
    """Engine capabilities do not determine ownership of a referenced table."""
    return engine_name(create).lower() != "distributed"


def inspect_existing_layout(
    connection: Any, table: str, *, require_owned: bool = False, **scopes: Any
) -> ExistingClickHouseLayout:
    hook = getattr(connection, "outage_inspect_existing_layout", None)
    if callable(hook) and not require_owned:
        layout = hook(table)
        if layout is None:
            message = f"ClickHouse table {table} does not exist."
            raise InvalidSqlInputError(message)
        return cast("ExistingClickHouseLayout", layout)
    with local_sql(connection):
        return _inspect_existing_layout_local(
            connection, table, require_owned=require_owned, **scopes
        )


__all__ = ["ExistingClickHouseLayout", "inspect_existing_layout"]
