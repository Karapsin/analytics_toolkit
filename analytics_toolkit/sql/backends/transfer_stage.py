from __future__ import annotations

# ruff: noqa: EM101, I001, TRY003

from typing import Any, Callable

from .gp.stage import GP_IDENTIFIER_MAX_BYTES, _fit_identifier_bytes

TRINO_IDENTIFIER_MAX_CHARS = 128


def execute_transfer_materialization(
    adapter: Any,
    backend: str,
    connection: Any,
    sql: str,
) -> None:
    if backend == "trino":
        adapter.execute_materialization_command(connection, sql)
        return
    adapter.execute_command(connection, sql)


def normalize_unquoted_identifier(identifier: str, backend: str) -> str:
    if backend in {"gp", "trino"}:
        return identifier.lower()
    if backend == "ch":
        return identifier
    raise KeyError(backend)


def build_transfer_stage_tail(
    connection_type: str,
    transfer_staging_username: str | None,
    stage_suffix: str,
) -> str:
    if connection_type == "gp":
        return stage_suffix
    if connection_type in {"trino", "ch"}:
        return _regular_tail(transfer_staging_username, stage_suffix)
    raise KeyError(connection_type)


def fit_hashed_stage_identifier(
    connection_type: str,
    prefix: str,
    readable_base: str,
    tail: str,
) -> str:
    fitter: Callable[[str, str, str], str] = {
        "gp": _fit_gp_hashed_stage_identifier,
        "trino": _fit_trino_hashed_stage_identifier,
        "ch": _unlimited_hashed_stage_identifier,
    }[connection_type]
    return fitter(prefix, readable_base, tail)


def collision_stage_suffix(
    connection_type: str,
    preferred_suffix: str,
    random_hex: str,
) -> str:
    if connection_type == "gp":
        return f"{preferred_suffix}{random_hex[:5]}"
    if connection_type in {"trino", "ch"}:
        return f"{preferred_suffix}__c_{random_hex[:8]}"
    raise KeyError(connection_type)


def build_source_snapshot_sqls(
    backend: str,
    snapshot_table: str,
    snapshot_select_sql: str,
    slice_column: str,
    ordinal_column: str,
) -> tuple[str, tuple[str, ...]]:
    builder = {
        "gp": _gp_source_snapshot_sqls,
        "ch": _ch_source_snapshot_sqls,
        "trino": _trino_source_snapshot_sqls,
    }[backend]
    return builder(
        snapshot_table,
        snapshot_select_sql,
        slice_column,
        ordinal_column,
    )


def _regular_tail(username: str | None, suffix: str) -> str:
    if username:
        return f"__analytics_toolkit_{username}__stage__{suffix}"
    return f"__stage__{suffix}"


def _fit_gp_hashed_stage_identifier(
    prefix: str,
    readable_base: str,
    tail: str,
) -> str:
    available = GP_IDENTIFIER_MAX_BYTES - len(prefix.encode()) - len(tail.encode())
    if available < 0:
        raise ValueError(
            "Destination hash and stage identity components are too long for Greenplum identifiers."
        )
    return f"{prefix}{_fit_identifier_bytes(readable_base, available)}{tail}"


def _unlimited_hashed_stage_identifier(
    prefix: str,
    readable_base: str,
    tail: str,
) -> str:
    return f"{prefix}{readable_base}{tail}"


def _fit_trino_hashed_stage_identifier(
    prefix: str,
    readable_base: str,
    tail: str,
) -> str:
    available = TRINO_IDENTIFIER_MAX_CHARS - len(prefix) - len(tail)
    if available < 0:
        raise ValueError(
            "Destination hash and stage identity components are too long for Trino identifiers."
        )
    return f"{prefix}{readable_base[:available]}{tail}"


def _gp_source_snapshot_sqls(
    table: str,
    select_sql: str,
    slice_column: str,
    ordinal_column: str,
) -> tuple[str, tuple[str, ...]]:
    return (
        f"CREATE TABLE {table} AS {select_sql} DISTRIBUTED RANDOMLY",
        (
            f"CREATE INDEX ON {table} ({slice_column}, {ordinal_column})",
            f"ANALYZE {table}",
        ),
    )


def _ch_source_snapshot_sqls(
    table: str,
    select_sql: str,
    slice_column: str,
    ordinal_column: str,
) -> tuple[str, tuple[str, ...]]:
    return (
        f"CREATE TABLE {table} ENGINE = MergeTree "
        f"ORDER BY ({slice_column}, {ordinal_column}) AS {select_sql}",
        (),
    )


def _trino_source_snapshot_sqls(
    table: str,
    select_sql: str,
    slice_column: str,
    ordinal_column: str,
) -> tuple[str, tuple[str, ...]]:
    del slice_column, ordinal_column
    return f"CREATE TABLE {table} AS {select_sql}", ()
