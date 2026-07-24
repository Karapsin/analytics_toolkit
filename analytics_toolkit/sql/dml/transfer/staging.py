from __future__ import annotations

import re
import warnings
from typing import Any
from typing import Sequence

from ...backends import get_backend_adapter
from ...connection.config import get_connection_config
from ...connection.errors import InvalidSqlInputError
from ...connection.get_sql_connection import get_sql_connection
from ...execution.operation_runner import timed_public_sql_function
from ...execution.validation import validate_non_negative_number, validate_positive_int
from ..load.stage import build_stage_table_prefix, cleanup_stage_table_with_retry
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
    _validate_cleanup_retry_options(read_retry_cnt, timeout_increment)
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
    _validate_cleanup_retry_options(read_retry_cnt, timeout_increment)
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


def _validate_cleanup_retry_options(
    read_retry_cnt: int,
    timeout_increment: int | float,
) -> None:
    validate_positive_int(read_retry_cnt, "read_retry_cnt")
    validate_non_negative_number(timeout_increment, "timeout_increment")


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
    return get_backend_adapter(backend).query_transfer_stage_table_names(
        connection["connection"],
        connection_key=db_key,
        transfer_staging_schema=transfer_staging_schema,
        table_pattern=table_pattern,
    )


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

    return get_backend_adapter(transfer_backend).qualify_transfer_stage_table_name(
        db_key,
        transfer_staging_schema,
        table_name,
    )


def _sanitize_transfer_staging_username(value: str) -> str:
    username = re.sub(r"[^0-9A-Za-z_]+", "_", value.strip())
    username = re.sub(r"_+", "_", username).strip("_")
    return username or "user"


def _build_user_stage_marker(transfer_staging_username: str) -> str:
    return f"__analytics_toolkit_{transfer_staging_username}__stage__"
