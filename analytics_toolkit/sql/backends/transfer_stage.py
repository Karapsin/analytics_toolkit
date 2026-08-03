from __future__ import annotations

# ruff: noqa: EM101, I001, TRY003

from typing import Any

from .gp.stage import GP_IDENTIFIER_MAX_BYTES, _fit_identifier_bytes


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
    if connection_type not in {"gp", "trino", "ch"}:
        raise KeyError(connection_type)
    del transfer_staging_username
    return stage_suffix


def fit_hashed_stage_identifier(
    connection_type: str,
    prefix: str,
    readable_base: str,
    tail: str,
) -> str:
    if connection_type not in {"gp", "trino", "ch"}:
        raise KeyError(connection_type)
    return _fit_gp_hashed_stage_identifier(prefix, readable_base, tail)


def collision_stage_suffix(
    connection_type: str,
    preferred_suffix: str,
    random_hex: str,
) -> str:
    if connection_type not in {"gp", "trino", "ch"}:
        raise KeyError(connection_type)
    return f"{preferred_suffix}{random_hex[:5]}"


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
