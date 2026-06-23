from __future__ import annotations

import re
import warnings
from typing import Any
from typing import Sequence

from ...connection.config import get_connection_config
from ...connection.errors import InvalidSqlInputError
from ...connection.get_sql_connection import get_sql_connection
from ...execution.operation_runner import timed_public_sql_function
from ..load.stage import build_stage_table_prefix, cleanup_stage_table_with_retry
from ..table._basic_ops import split_trino_table_name
from .runtime.retry import replace_connection, rollback_quietly, run_with_retry

_DEFAULT_TIMEOUT_INCREMENT = 5
_WARNING_KEY_PREFIX = "cleanup_stale_stage_tables_no_schema::"

_warned_transfer_staging_schema_cleanup: set[str] = set()


@timed_public_sql_function
def cleanup_stale_stage_tables(
    db_key: str,
    target_table: str | None = None,
    *,
    stage_tables: Sequence[str] | None = None,
    clean_all: bool = False,
    read_retry_cnt: int = 5,
    timeout_increment: int | float = _DEFAULT_TIMEOUT_INCREMENT,
    query_label: str | None = None,
) -> None:
    connection_ref = {"connection": get_sql_connection(db_key)}
    try:
        cleanup_stale_stage_tables_with_connection(
            db_key=db_key,
            target_table=target_table,
            connection_ref=connection_ref,
            stage_tables=stage_tables,
            clean_all=clean_all,
            read_retry_cnt=read_retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        )
    finally:
        try:
            connection_ref["connection"].close()
        except Exception:
            pass


def cleanup_stale_stage_tables_with_connection(
    db_key: str,
    target_table: str | None,
    connection_ref: dict[str, Any],
    *,
    stage_tables: Sequence[str] | None = None,
    clean_all: bool = False,
    read_retry_cnt: int = 5,
    timeout_increment: int | float = _DEFAULT_TIMEOUT_INCREMENT,
    query_label: str | None = None,
) -> None:
    config = get_connection_config(db_key)
    transfer_staging_schema = config.transfer_staging_schema
    transfer_staging_username = _sanitize_transfer_staging_username(config.user)

    if clean_all and stage_tables is not None:
        raise InvalidSqlInputError(
            "clean_all=True cannot be combined with explicit stage_tables."
        )

    if clean_all:
        if transfer_staging_schema is None:
            _warn_transfer_staging_schema_cleanup_not_configured(config.connection_key)
            return

        target_stages = _find_all_user_transfer_stage_tables(
            db_key=db_key,
            connection=connection_ref,
            transfer_staging_schema=transfer_staging_schema,
            transfer_staging_username=transfer_staging_username,
        )
    elif stage_tables is None:
        if transfer_staging_schema is None:
            _warn_transfer_staging_schema_cleanup_not_configured(config.connection_key)
            return

        if target_table is None:
            raise InvalidSqlInputError(
                "target_table is required when clean_all=False and stage_tables=None."
            )

        target_stages = _find_matching_transfer_stage_tables(
            db_key=db_key,
            target_table=target_table,
            connection=connection_ref,
            transfer_staging_schema=transfer_staging_schema,
            transfer_staging_username=transfer_staging_username,
        )
    else:
        target_stages = [
            _qualify_staging_table_name(
                db_key=db_key,
                transfer_backend=config.backend,
                transfer_staging_schema=transfer_staging_schema,
                table_name=str(table_name).strip(),
            )
            for table_name in stage_tables
            if str(table_name).strip()
        ]

    for table_name in target_stages:
        cleanup_stage_table_with_retry(
            config.backend,
            config.connection_key,
            connection_ref,
            table_name,
            retry_fn=run_with_retry,
            retry_cnt=read_retry_cnt,
            timeout_increment=timeout_increment,
            rollback_fn=rollback_quietly,
            replace_connection_fn=replace_connection,
            query_label=query_label,
        )


def _warn_transfer_staging_schema_cleanup_not_configured(db_key: str) -> None:
    warning_key = _WARNING_KEY_PREFIX + db_key
    if warning_key in _warned_transfer_staging_schema_cleanup:
        return

    warnings.warn(
        "clean_transfer_staging_schema is enabled, "
        "but transfer_staging_schema is not configured for the target connection",
    )
    _warned_transfer_staging_schema_cleanup.add(warning_key)


def _find_all_user_transfer_stage_tables(
    db_key: str,
    connection: dict[str, Any],
    transfer_staging_schema: str,
    transfer_staging_username: str,
) -> list[str]:
    config = get_connection_config(db_key)
    marker = _build_user_stage_marker(transfer_staging_username)
    table_names = _query_transfer_stage_table_names(
        db_key=db_key,
        backend=config.backend,
        connection=connection,
        transfer_staging_schema=transfer_staging_schema,
        table_pattern=f"%{marker}%",
    )

    return [
        _qualify_staging_table_name(
            db_key=db_key,
            transfer_backend=config.backend,
            transfer_staging_schema=transfer_staging_schema,
            table_name=table_name,
        )
        for table_name in table_names
        if marker in table_name
    ]


def _find_matching_transfer_stage_tables(
    db_key: str,
    target_table: str,
    connection: dict[str, Any],
    transfer_staging_schema: str,
    transfer_staging_username: str,
) -> list[str]:
    config = get_connection_config(db_key)
    prefix = build_stage_table_prefix(
        config.backend,
        target_table,
        transfer_staging_username,
    )
    like_prefix = f"{prefix}%"
    table_names = _query_transfer_stage_table_names(
        db_key=db_key,
        backend=config.backend,
        connection=connection,
        transfer_staging_schema=transfer_staging_schema,
        table_pattern=like_prefix,
    )

    return [
        _qualify_staging_table_name(
            db_key=db_key,
            transfer_backend=config.backend,
            transfer_staging_schema=transfer_staging_schema,
            table_name=table_name,
        )
        for table_name in table_names
        if table_name.startswith(prefix)
    ]


def _query_transfer_stage_table_names(
    db_key: str,
    backend: str,
    connection: dict[str, Any],
    transfer_staging_schema: str,
    table_pattern: str,
) -> list[str]:
    if backend == "gp":
        return _query_gp_stage_tables(
            transfer_staging_schema=transfer_staging_schema,
            table_prefix=table_pattern,
            connection=connection["connection"],
        )
    if backend == "trino":
        return _query_trino_stage_tables(
            transfer_staging_schema=transfer_staging_schema,
            connection_key=db_key,
            table_prefix=table_pattern,
            connection=connection["connection"],
        )
    if backend == "ch":
        return _query_ch_stage_tables(
            transfer_staging_schema=transfer_staging_schema,
            connection=connection["connection"],
        )
    raise ValueError(f"Unsupported transfer backend for staging cleanup: {backend}")


def _query_gp_stage_tables(
    transfer_staging_schema: str,
    table_prefix: str,
    *,
    connection: Any,
) -> list[str]:
    cursor = _require_cursor(connection)
    try:
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name LIKE %s
            """.strip(),
            (transfer_staging_schema, table_prefix),
        )
        return [str(row[0]) for row in (cursor.fetchall() or [])]
    finally:
        cursor.close()


def _query_trino_stage_tables(
    transfer_staging_schema: str,
    connection_key: str,
    table_prefix: str,
    *,
    connection: Any,
) -> list[str]:
    catalog_name, schema_name, _ = split_trino_table_name(
        f"{transfer_staging_schema}.__analytics_toolkit_stage_marker__",
        connection_key=connection_key,
    )
    cursor = _require_cursor(connection)
    try:
        cursor.execute(
            f"""
            SELECT table_name
            FROM {catalog_name}.information_schema.tables
            WHERE table_schema = ?
              AND table_name LIKE ?
            """.strip(),
            (schema_name, table_prefix),
        )
        return [str(row[0]) for row in (cursor.fetchall() or [])]
    finally:
        cursor.close()


def _query_ch_stage_tables(
    transfer_staging_schema: str,
    *,
    connection: Any,
) -> list[str]:
    result = _require_query(connection).query(
        "SELECT name FROM system.tables WHERE database = "
        f"{_quote_sql_literal(transfer_staging_schema)}"
    )
    return [str(row[0]) for row in (result.result_rows or [])]


def _is_fully_qualified_stage_table_name(
    table_name: str,
) -> bool:
    return "." in table_name.strip()


def _qualify_staging_table_name(
    db_key: str,
    transfer_backend: str,
    transfer_staging_schema: str | None,
    table_name: str,
) -> str:
    if _is_fully_qualified_stage_table_name(table_name):
        return table_name.strip()
    if transfer_staging_schema is None:
        raise InvalidSqlInputError(
            "Unqualified stage table names require transfer_staging_schema."
        )

    if transfer_backend == "ch":
        return f"{transfer_staging_schema}.{table_name}"

    if transfer_backend == "trino":
        catalog_name, schema_name, _ = split_trino_table_name(
            f"{transfer_staging_schema}.__analytics_toolkit_stage_marker__",
            connection_key=db_key,
        )
        return f"{catalog_name}.{schema_name}.{table_name}"

    return f"{transfer_staging_schema}.{table_name}"


def _sanitize_transfer_staging_username(value: str) -> str:
    username = re.sub(r"[^0-9A-Za-z_]+", "_", value.strip())
    username = re.sub(r"_+", "_", username).strip("_")
    return username or "user"


def _build_user_stage_marker(transfer_staging_username: str) -> str:
    return f"__analytics_toolkit_{transfer_staging_username}__stage__"


def _quote_sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _require_cursor(connection: object) -> Any:
    if not hasattr(connection, "cursor"):
        raise TypeError("Target connection must provide a cursor() method.")
    return connection.cursor()


def _require_query(connection: object):
    if not hasattr(connection, "query"):
        raise TypeError("Target connection must provide a query() method.")
    return connection
