from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pandas as pd
from sqlglot import exp, parse_one

from ...backend_adapters import get_backend_adapter
from ...core.identifiers import sqlglot_dialect as _registry_sqlglot_dialect
from analytics_toolkit.general import time_print
from ...ddl.api import _create_sql_table_with_connection
from ..table.maintenance import drop_table, drop_table_with_retry
from ..table._basic_ops import table_exists


STAGE_TABLE_NAME_MAX_ATTEMPTS = 10
STAGE_TABLE_RANDOM_SUFFIX_LENGTH = 8


def create_stage_table(
    connection_type: str,
    connection: Any,
    target_table: str,
    batch: pd.DataFrame,
    column_types: Mapping[str, str] | None = None,
    table_schema: Mapping[str, str] | None = None,
    gp_distributed_by_key: list[str] | None = None,
    connection_key: str | None = None,
    query_label: str | None = None,
    transfer_staging_schema: str | None = None,
    transfer_staging_username: str | None = None,
    random_suffix: str | None = None,
) -> str:
    max_attempts = 1 if random_suffix is not None else STAGE_TABLE_NAME_MAX_ATTEMPTS
    for attempt in range(1, max_attempts + 1):
        stage_table = build_stage_table_name(
            connection_type,
            target_table,
            transfer_staging_schema=transfer_staging_schema,
            transfer_staging_username=transfer_staging_username,
            random_suffix=random_suffix,
        )
        if table_exists(
            connection_type,
            connection,
            stage_table,
            connection_key=connection_key or connection_type,
        ):
            if random_suffix is not None:
                raise RuntimeError(
                    f"Stage table name collision detected for {stage_table}."
                )
            time_print(
                f"Stage table name collision detected for {stage_table}; "
                f"retrying with a new name ({attempt}/{STAGE_TABLE_NAME_MAX_ATTEMPTS})"
            )
            continue

        create_kwargs: dict[str, Any] = {}
        if query_label is not None:
            create_kwargs["query_label"] = query_label
        create_schema = table_schema or column_types
        if create_schema is not None:
            create_kwargs["table_schema"] = create_schema
        _create_sql_table_with_connection(
            connection_type,
            connection,
            stage_table,
            None if create_schema is not None else batch,
            connection_key=connection_key or connection_type,
            gp_distributed_by_key=gp_distributed_by_key,
            **create_kwargs,
        )
        return stage_table

    raise RuntimeError(
        "Could not generate a unique stage table name after "
        f"{STAGE_TABLE_NAME_MAX_ATTEMPTS} attempts."
    )


def cleanup_stage_table(
    connection_type: str,
    connection: Any,
    stage_table: str,
    *,
    query_label: str | None = None,
    if_exists: bool = True,
) -> None:
    drop_table(
        connection_type,
        connection,
        stage_table,
        query_label=query_label,
        if_exists=if_exists,
    )


def cleanup_stage_table_with_retry(
    connection_type: str,
    connection_key: str,
    connection_ref: dict[str, Any],
    stage_table: str,
    *,
    retry_fn: Any,
    retry_cnt: int,
    timeout_increment: int | float,
    rollback_fn: Any,
    replace_connection_fn: Any,
    query_label: str | None = None,
    if_exists: bool = True,
) -> None:
    drop_table_with_retry(
        connection_type,
        connection_key,
        connection_ref,
        stage_table,
        retry_fn=retry_fn,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        rollback_fn=rollback_fn,
        replace_connection_fn=replace_connection_fn,
        query_label=query_label,
        if_exists=if_exists,
    )


def build_stage_table_name(
    connection_type: str,
    table_name: str,
    transfer_staging_schema: str | None = None,
    transfer_staging_username: str | None = None,
    random_suffix: str | None = None,
) -> str:
    dialect = sqlglot_dialect(connection_type)
    table = parse_one(table_name, read=dialect, into=exp.Table)
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        raise ValueError(f"Invalid target table name: {table_name}")

    if transfer_staging_schema is not None:
        staging_schema_table = parse_one(
            f"{transfer_staging_schema}.__analytics_toolkit_stage_marker__",
            read=dialect,
            into=exp.Table,
        )
        if (
            not isinstance(staging_schema_table, exp.Table)
            or not isinstance(staging_schema_table.this, exp.Identifier)
        ):
            raise ValueError(
                f"Invalid transfer_staging_schema for {connection_type}: "
                f"{transfer_staging_schema}"
            )
        table.set("catalog", staging_schema_table.args.get("catalog"))
        table.set("db", staging_schema_table.args.get("db") or staging_schema_table.this)

    stage_suffix = random_suffix or uuid.uuid4().hex[:8]
    stage_identifier = _build_stage_identifier(
        connection_type,
        table,
        transfer_staging_username,
        stage_suffix,
    )
    stage_table = table.copy()
    stage_table.set("this", stage_identifier)
    return stage_table.sql(dialect=dialect)


def build_stage_table_prefix(
    connection_type: str,
    table_name: str,
    transfer_staging_username: str | None,
) -> str:
    dialect = sqlglot_dialect(connection_type)
    table = parse_one(table_name, read=dialect, into=exp.Table)
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        raise ValueError(f"Invalid target table name: {table_name}")

    base_identifier = _stage_base_identifier(
        connection_type,
        str(table.this.this),
        transfer_staging_username,
        stage_suffix="x" * STAGE_TABLE_RANDOM_SUFFIX_LENGTH,
    )
    if transfer_staging_username:
        return (
            f"{base_identifier}__analytics_toolkit_"
            f"{transfer_staging_username}__stage__"
        )
    return f"{base_identifier}__stage__"


def _build_stage_identifier(
    connection_type: str,
    table: exp.Table,
    transfer_staging_username: str | None,
    stage_suffix: str,
) -> exp.Identifier:
    base_identifier = _stage_base_identifier(
        connection_type,
        str(table.this.this),
        transfer_staging_username,
        stage_suffix=stage_suffix,
    )
    if transfer_staging_username:
        identifier = (
            f"{base_identifier}__analytics_toolkit_{transfer_staging_username}"
            f"__stage__{stage_suffix}"
        )
    else:
        identifier = f"{base_identifier}__stage__{stage_suffix}"
    return exp.to_identifier(
        identifier,
        quoted=bool(table.this.args.get("quoted")),
    )


def _stage_base_identifier(
    connection_type: str,
    base_identifier: str,
    transfer_staging_username: str | None,
    stage_suffix: str,
) -> str:
    return get_backend_adapter(connection_type).stage_base_identifier(
        base_identifier,
        transfer_staging_username,
        stage_suffix,
    )


def sqlglot_dialect(connection_type: str) -> str:
    return _registry_sqlglot_dialect(connection_type)
