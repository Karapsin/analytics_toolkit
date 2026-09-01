from __future__ import annotations

# ruff: noqa: BLE001, EM101, TRY003, TRY301
from typing import Any
from uuid import uuid4

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError
from analytics_toolkit.sql.ddl.identifiers import (
    _add_table_identifier_suffix,
    _identifier_name,
    _parse_table_name,
)

from .gp.stage import GP_IDENTIFIER_MAX_BYTES, _fit_identifier_bytes
from .models import StageFinalizationRequest, StageTargetTableRequest


def finalize_existing_stage_replace(
    adapter: Any,
    request: StageFinalizationRequest,
) -> bool:
    if not (
        request.replace_target_table and request.target_exists and request.write_mode == "replace"
    ):
        return False
    if adapter.backend not in {"gp", "trino"}:
        return False

    token = uuid4().hex[:12]
    replacement = _replacement_artifact_table(
        adapter,
        request.target_table,
        f"__replace_{token}",
    )
    backup = _replacement_artifact_table(
        adapter,
        request.target_table,
        f"__backup_{token}",
    )
    adapter.ensure_stage_target_table(
        StageTargetTableRequest(
            connection=request.connection,
            target_table=replacement,
            sample_batch=request.sample_batch,
            target_column_types=request.target_column_types,
            gp_distributed_by_key=request.gp_distributed_by_key,
            gp_partitions=request.gp_partitions,
            partition_by=request.partition_by,
            order_by=request.order_by,
            ch_engine=request.ch_engine,
            ch_cluster=request.ch_cluster,
            ch_sharding_key=request.ch_sharding_key,
            query_label=request.query_label,
            connection_key=request.connection_key,
            ch_only_shard=request.ch_only_shard,
        )
    )
    try:
        adapter.insert_from_table(
            request.connection,
            replacement,
            request.stage_table,
            column_types=request.insert_column_types,
            query_label=request.query_label,
        )
        expected_rows = adapter.count_table_rows(request.connection, request.stage_table)
        replacement_rows = adapter.count_table_rows(request.connection, replacement)
        if replacement_rows != expected_rows:
            raise RuntimeError("Replacement row count does not match the staged row count.")
        if adapter.supports_transactions:
            _transactional_cutover(adapter, request, replacement, backup)
        else:
            _reversible_cutover(adapter, request, replacement, backup, expected_rows)
    except AmbiguousSqlReplaceError:
        raise
    except Exception:
        _best_effort_drop(adapter, request, replacement)
        raise
    return True


def _replacement_artifact_table(adapter: Any, table_name: str, suffix: str) -> str:
    if adapter.backend != "gp":
        return _add_table_identifier_suffix(table_name, suffix, adapter.sqlglot_dialect)

    table = _parse_table_name(table_name, adapter.sqlglot_dialect)
    table_identifier = table.this
    max_base_bytes = GP_IDENTIFIER_MAX_BYTES - len(suffix.encode())
    base_identifier = _fit_identifier_bytes(
        _identifier_name(table_identifier),
        max_base_bytes,
    )
    artifact_identifier = table_identifier.copy()
    artifact_identifier.set("this", f"{base_identifier}{suffix}")
    artifact_table = table.copy()
    artifact_table.set("this", artifact_identifier)
    return artifact_table.sql(dialect=adapter.sqlglot_dialect)


def _transactional_cutover(
    adapter: Any,
    request: StageFinalizationRequest,
    replacement: str,
    backup: str,
) -> None:
    cursor = request.connection.cursor()
    try:
        cursor.execute(_rename_sql(adapter, request.target_table, backup))
        cursor.execute(_rename_sql(adapter, replacement, request.target_table))
        cursor.execute(adapter.drop_table_sql(backup, query_label=request.query_label))
    except Exception:
        request.connection.rollback()
        raise
    finally:
        cursor.close()
    try:
        request.connection.commit()
    except Exception as exc:
        raise AmbiguousSqlReplaceError(
            "The transactional replacement commit failed; the destination may contain "
            "either the old or the new complete table."
        ) from exc


def _reversible_cutover(
    adapter: Any,
    request: StageFinalizationRequest,
    replacement: str,
    backup: str,
    expected_rows: int,
) -> None:
    old_moved = False
    new_moved = False
    try:
        adapter.execute_command(
            request.connection,
            _rename_sql(adapter, request.target_table, backup),
        )
        old_moved = True
        adapter.execute_command(
            request.connection,
            _rename_sql(adapter, replacement, request.target_table),
        )
        new_moved = True
        if adapter.count_table_rows(request.connection, request.target_table) != expected_rows:
            raise RuntimeError("Final replacement row count does not match the staged row count.")
    except Exception:
        try:
            if new_moved:
                adapter.execute_command(
                    request.connection,
                    _rename_sql(adapter, request.target_table, replacement),
                )
            if old_moved:
                adapter.execute_command(
                    request.connection,
                    _rename_sql(adapter, backup, request.target_table),
                )
        except Exception as rollback_exc:
            raise AmbiguousSqlReplaceError(
                "Replacement and automatic rollback both failed; replacement artifacts "
                "were preserved for recovery."
            ) from rollback_exc
        raise

    try:
        adapter.drop_table(
            request.connection,
            backup,
            query_label=request.query_label,
        )
    except Exception as exc:
        time_print(
            f"Could not remove replacement backup {backup}: {type(exc).__name__}",
            level="warning",
        )


def _rename_sql(adapter: Any, source: str, destination: str) -> str:
    if adapter.backend == "trino":
        return f"ALTER TABLE {source} RENAME TO {destination}"
    destination_table = _parse_table_name(destination, adapter.sqlglot_dialect)
    destination_relation = adapter.quote_identifier(_identifier_name(destination_table.this))
    return f"ALTER TABLE {source} RENAME TO {destination_relation}"


def _best_effort_drop(
    adapter: Any,
    request: StageFinalizationRequest,
    table: str,
) -> None:
    try:
        adapter.drop_table(
            request.connection,
            table,
            query_label=request.query_label,
        )
    except Exception as exc:
        time_print(
            f"Could not remove replacement artifact {table}: {type(exc).__name__}",
            level="warning",
        )


__all__ = ["finalize_existing_stage_replace"]
