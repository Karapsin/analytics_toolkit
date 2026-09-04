from __future__ import annotations

from collections.abc import Callable, Sequence
from string import Formatter
from typing import Any

from . import operations as _operations
from . import insert as _insert
from . import parquet_stage as _parquet_stage
from .. import dataframe_types as _dataframe_types
from .. import source_schema as _source_schema
from ..base import _apply_query_label
from ..models import SourceColumn
from ..utils import sql_literal
from ..utils import sql_in_list as _sql_in_list
from ..utils import user_filter as _user_filter
from ..dbapi import DbApiBackendAdapter


_TRINO_MAX_DECIMAL_PRECISION = 38


class TrinoAdapter(DbApiBackendAdapter):
    display_name = "Trino"
    sqlglot_dialect = "trino"
    identifier_quote = '"'
    supports_transactions = False
    supports_analyze = True
    supports_distributed_tables = False
    truncate_semantics = "DELETE FROM"
    drop_semantics = "DROP TABLE IF EXISTS"
    create_semantics = "CREATE TABLE WITH parquet/object-store layout"
    type_family = "trino"
    upsert_strategy = "partition_replace"
    requires_upsert_partition_column = True
    requires_upsert_partition_drop_template = True
    supports_show_tables_catalog_filter = True
    forbidden_airflow_file_override_fields = _parquet_stage.FORBIDDEN_CREDENTIAL_FIELDS
    build_execute_create_as_sqls = _operations.build_execute_create_as_sqls

    def __init__(self) -> None:
        super().__init__(backend="trino", commit_commands=False)

    def execute_materialization_command(self, connection: Any, sql: str) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute(sql)
            cursor.fetchall()
        finally:
            cursor.close()

    def explicit_create_property_overrides(
        self,
        partition_by: Sequence[str] | str | None,
        order_by: Sequence[str] | str | None,
    ) -> dict[str, Any]:
        overrides: dict[str, Any] = {}
        partition_entries = _normalize_trino_property_entries(partition_by, "partition_by")
        order_entries = _normalize_trino_property_entries(order_by, "order_by")
        if partition_entries:
            overrides["partitioning"] = partition_entries
        if order_entries:
            overrides["sorted_by"] = order_entries
        return overrides

    def build_connection_config(
        self,
        connection_key: str,
        raw_config: dict[str, Any],
    ) -> Any:
        from .config import build_config

        return build_config(connection_key, raw_config)

    def copy_airflow_fields(
        self,
        raw_config: dict[str, Any],
        extras: dict[str, Any],
        connection: Any,
        copy_extra_fields: Callable[[dict[str, Any], dict[str, Any], Sequence[str]], None],
        set_if_not_none: Callable[[dict[str, Any], str, Any], None],
    ) -> None:
        del connection, set_if_not_none
        copy_extra_fields(
            raw_config,
            extras,
            _parquet_stage.AIRFLOW_EXTRA_FIELDS,
        )
        if isinstance(raw_config.get("verify"), bool):
            raw_config["verify"] = str(raw_config["verify"]).lower()

    def open_connection(
        self,
        config: Any,
        *,
        parse_verify_value: Callable[[str], bool | str],
        resolve_ca_certs: Callable[[str, list[str]], str | None],
        resolve_single_cert_path: Callable[[str, str, str], Any],
        resolve_ch_ca_certs: Callable[[Any], str | None],
    ) -> Any:
        del resolve_single_cert_path, resolve_ch_ca_certs
        from .config import open_connection

        return open_connection(
            config,
            parse_verify_value=parse_verify_value,
            resolve_ca_certs=resolve_ca_certs,
        )

    def build_create_table_sqls(
        self,
        *,
        table_name: str,
        joined_columns: str,
        gp_distributed_by_key: list[str] | None,
        gp_partitions: Any = None,
        partition_by: Sequence[str] | str | None,
        order_by: Sequence[str] | str | None,
        ch_engine: str,
        ch_cluster: str,
        ch_sharding_key: str,
        ch_distributed_table: bool,
        ch_only_shard: bool,
        ch_replace_table: bool,
        if_not_exists: bool = False,
    ) -> list[str]:
        del (
            gp_distributed_by_key,
            gp_partitions,
            ch_engine,
            ch_cluster,
            ch_sharding_key,
            ch_distributed_table,
            ch_only_shard,
            ch_replace_table,
        )
        properties = _build_trino_table_properties(
            partition_by=partition_by,
            order_by=order_by,
        )
        create = "CREATE TABLE IF NOT EXISTS" if if_not_exists else "CREATE TABLE"
        return [f"{create} {table_name} ({joined_columns}) WITH ({properties})"]

    def table_exists(
        self,
        connection: Any,
        table_name: str,
        *,
        connection_key: str,
    ) -> bool:
        catalog, schema_name, relation_name = split_trino_table_name(
            table_name,
            connection_key=connection_key,
        )
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"""
                SELECT 1
                FROM {catalog}.information_schema.tables
                WHERE table_schema = ?
                  AND table_name = ?
                """.strip(),
                (schema_name, relation_name),
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()

    def clear_table_sqls(
        self,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> list[str]:
        return [_apply_query_label(f"DELETE FROM {table_name}", query_label)]

    def transfer_replace_existing_non_ch(self) -> str:
        return "drop"

    def requires_load_target_column_metadata(
        self,
        *,
        write_mode: str,
        original_target_exists: bool,
    ) -> bool:
        del write_mode, original_target_exists
        return True

    def validate_trino_insert_chunk_size_option(
        self,
        value: int | None,
        *,
        option_owner: str,
    ) -> None:
        del option_owner
        if value is not None and value <= 0:
            raise ValueError("trino_insert_chunk_size must be a positive integer.")

    build_show_tables_query = _operations.build_show_tables_query
    extract_table_ddl = _operations.extract_table_ddl
    target_connection_defaults = _operations.target_connection_defaults
    build_materialize_transfer_source_sql = _operations.build_materialize_transfer_source_sql
    resolve_transfer_staging_mode = _operations.resolve_transfer_staging_mode
    resolve_transfer_stage_column_types = _operations.resolve_transfer_stage_column_types
    validate_drop_partitions_options = _operations.validate_drop_partitions_options
    build_drop_partitions_sqls = _operations.build_drop_partitions_sqls
    query_transfer_stage_table_names = _operations.query_transfer_stage_table_names
    qualify_transfer_stage_table_name = _operations.qualify_transfer_stage_table_name
    infer_dataframe_column_type = _dataframe_types.infer_trino_dataframe_column_type
    build_parquet_stage_table_sql = _parquet_stage.build_parquet_stage_table_sql
    infer_parquet_stage_column_types_from_rows = (
        _parquet_stage.infer_parquet_stage_column_types_from_rows
    )
    parquet_stage_target_table_base = _parquet_stage.parquet_stage_target_table_base

    def estimate_source_rows(
        self,
        connection: Any,
        source_sql: str,
        *,
        query_label: str | None = None,
    ) -> int | None:
        from ..source_estimate import _estimate_trino_source_rows

        return _estimate_trino_source_rows(
            connection,
            source_sql,
            query_label=query_label,
        )

    def build_dataframe_batch_insert_sql(
        self,
        table_name: str,
        columns: Sequence[str],
        *,
        row_count: int,
        query_label: str | None = None,
    ) -> str:
        if row_count <= 0:
            raise ValueError("row_count must be a positive integer.")

        row_placeholders = f"({', '.join('?' for _ in columns)})"
        values_sql = ", ".join(row_placeholders for _ in range(row_count))
        return _apply_query_label(
            f"INSERT INTO {table_name} ({self.column_list_sql(columns)}) VALUES {values_sql}",
            query_label,
        )

    def get_table_column_types(
        self,
        connection: Any,
        table_name: str,
        *,
        connection_key: str,
    ) -> dict[str, str]:
        catalog, schema_name, relation_name = split_trino_table_name(
            table_name,
            connection_key=connection_key,
        )
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"""
                SELECT column_name, data_type
                FROM {catalog}.information_schema.columns
                WHERE table_schema = ?
                  AND table_name = ?
                ORDER BY ordinal_position
                """.strip(),
                (schema_name, relation_name),
            )
            return {
                str(column_name): str(data_type) for column_name, data_type in cursor.fetchall()
            }
        finally:
            cursor.close()

    def inspect_source_query_schema(
        self,
        connection: Any,
        query: str,
    ) -> list[SourceColumn]:
        return _source_schema.inspect_dbapi_source_schema(
            connection,
            query,
            type_code_name=self.type_code_name,
        )

    def map_source_type_to_target(self, column: SourceColumn) -> str:
        source_type = _source_schema.normalize_type_name(column.native_type)
        precision, scale = _source_schema.type_precision_scale(column, source_type)
        kind = _source_schema.classify_source_type(source_type)
        return _map_to_trino_type(kind, source_type, precision, scale)

    def map_same_backend_source_type_to_target(self, column: SourceColumn) -> str:
        source_type = _source_schema.normalize_type_name(column.native_type)
        if source_type and source_type != "unknown":
            return source_type
        return self.map_source_type_to_target(column)

    def build_upsert_stage_sqls(
        self,
        target_table: str,
        stage_table: str,
        *,
        columns: Sequence[str],
        key_columns: Sequence[str],
        column_types: dict[str, str] | None = None,
        ch_cluster: str = "{cluster}",
        ch_only_shard: bool = False,
        query_label: str | None = None,
        upsert_partition_column: str | None = None,
        final_stage_table: str | None = None,
        incoming_stage_tables: Sequence[str] | None = None,
        partition_values: Sequence[Any] | None = None,
        trino_partition_drop_sql_template: str | None = None,
    ) -> list[str]:
        del ch_cluster, ch_only_shard
        if upsert_partition_column is None or final_stage_table is None:
            raise ValueError(
                "upsert_partition_column and final_stage_table are required for "
                "Trino write_mode='upsert'."
            )
        return self.build_partition_replacement_upsert_sqls(
            target_table,
            stage_table,
            final_stage_table=final_stage_table,
            columns=columns,
            key_columns=key_columns,
            partition_column=upsert_partition_column,
            column_types=column_types,
            incoming_stage_tables=incoming_stage_tables,
            partition_values=partition_values,
            query_label=query_label,
            trino_partition_drop_sql_template=trino_partition_drop_sql_template,
        )

    def build_upsert_stage_placeholder_sqls(
        self,
        target_table: str,
        stage_table: str,
        *,
        key_columns: Sequence[str],
        ch_cluster: str = "{cluster}",
        ch_only_shard: bool = False,
        query_label: str | None = None,
        upsert_partition_column: str | None = None,
        final_stage_table: str | None = None,
        incoming_stage_tables: Sequence[str] | None = None,
        partition_values: Sequence[Any] | None = None,
        trino_partition_drop_sql_template: str | None = None,
    ) -> list[str]:
        del ch_cluster, ch_only_shard
        if upsert_partition_column is None or final_stage_table is None:
            raise ValueError(
                "upsert_partition_column and final_stage_table are required for "
                "Trino write_mode='upsert'."
            )

        return [
            self.build_preserved_target_rows_insert_sql(
                target_table,
                stage_table,
                final_stage_table=final_stage_table,
                columns=["<source query columns>"],
                key_columns=key_columns,
                partition_column=upsert_partition_column,
                incoming_stage_tables=incoming_stage_tables,
                query_label=query_label,
            ),
            self.build_incoming_rows_insert_sql(
                final_stage_table,
                stage_table,
                columns=["<source query columns>"],
                column_types=None,
                incoming_stage_tables=incoming_stage_tables,
                query_label=query_label,
            ),
            *self.build_drop_upsert_partition_sqls(
                target_table,
                partition_column=upsert_partition_column,
                partition_values=partition_values,
                query_label=query_label,
                trino_partition_drop_sql_template=trino_partition_drop_sql_template,
            ),
            self.build_insert_from_stage_placeholder_sql(
                target_table,
                final_stage_table,
                query_label=query_label,
            ),
        ]

    def build_drop_upsert_partition_sqls(
        self,
        target_table: str,
        *,
        partition_column: str,
        partition_values: Sequence[Any] | None,
        query_label: str | None = None,
        trino_partition_drop_sql_template: str | None = None,
        ch_cluster: str = "{cluster}",
        ch_only_shard: bool = False,
    ) -> list[str]:
        del ch_cluster, ch_only_shard
        template = _validate_trino_partition_drop_template(trino_partition_drop_sql_template)
        values = (
            list(partition_values)
            if partition_values is not None
            else ["<affected partition value>"]
        )
        return [
            _apply_query_label(
                template.format(
                    table=target_table,
                    partition_column=self.quote_identifier(partition_column),
                    partition_value=(
                        value
                        if isinstance(value, str) and value.startswith("<")
                        else sql_literal(value)
                    ),
                ),
                query_label,
            )
            for value in values
        ]

    def _build_merge_sql(
        self,
        target_table: str,
        stage_table: str,
        *,
        columns: Sequence[str],
        key_columns: Sequence[str],
    ) -> str:
        on_predicates = " AND ".join(
            self.null_safe_key_equality("target_dst", "stage_src", column_name)
            for column_name in key_columns
        )
        assignments = ",\n  ".join(
            f"{self.quote_identifier(column_name)} = stage_src.{self.quote_identifier(column_name)}"
            for column_name in columns
        )
        insert_columns = self.column_list_sql(columns)
        insert_values = ", ".join(
            f"stage_src.{self.quote_identifier(column_name)}" for column_name in columns
        )
        return (
            f"MERGE INTO {target_table} AS target_dst\n"
            f"USING {stage_table} AS stage_src\n"
            f"ON {on_predicates}\n"
            "WHEN MATCHED THEN UPDATE SET\n"
            f"  {assignments}\n"
            f"WHEN NOT MATCHED THEN INSERT ({insert_columns})\n"
            f"  VALUES ({insert_values})"
        )

    def _build_merge_placeholder_sql(
        self,
        target_table: str,
        stage_table: str,
        *,
        key_columns: Sequence[str],
    ) -> str:
        on_predicates = " AND ".join(
            self.null_safe_key_equality("target_dst", "stage_src", column_name)
            for column_name in key_columns
        )
        return (
            f"MERGE INTO {target_table} AS target_dst\n"
            f"USING {stage_table} AS stage_src\n"
            f"ON {on_predicates}\n"
            "WHEN MATCHED THEN UPDATE SET\n"
            "  <source query columns>\n"
            "WHEN NOT MATCHED THEN INSERT (<source query columns>)\n"
            "  VALUES (<source query columns>)"
        )

    def execute_sql(
        self,
        connection: Any,
        sql: str,
        *,
        print_queries: bool,
        gp_break_query: bool,
        gp_commit_each_statement: bool,
        progress: bool,
    ) -> Any:
        del gp_break_query, gp_commit_each_statement
        from analytics_toolkit.general import time_print
        from ...execution.query_timing import run_timed_query
        from ...dml.io.execute_sql import (
            _iterate_statements_with_progress,
            _maybe_print_query,
            _split_sql_statements,
        )

        cursor = connection.cursor()
        statements = _split_sql_statements(sql)
        time_print(f"Executing {len(statements)} statement(s)", backend=self.backend)
        statement: str | None = None
        try:
            for statement in _iterate_statements_with_progress(
                statements,
                self.backend,
                progress=progress,
            ):
                _maybe_print_query(statement, print_queries, split_preview=True)
                run_timed_query(
                    self.backend,
                    lambda statement=statement: cursor.execute(statement),
                )
        except Exception:
            failed_query = statement if statement is not None else sql
            time_print(f"Failed SQL:\n{failed_query}", backend=self.backend)
            raise
        finally:
            cursor.close()
        return None

    def execute_read_sql(
        self,
        connection: Any,
        statements: list[str],
        *,
        print_queries: bool,
        gp_break_query: bool,
        gp_commit_each_statement: bool,
        progress: bool,
    ) -> Any:
        del gp_break_query, gp_commit_each_statement
        from analytics_toolkit.general import time_print
        from ...dml.io.execute_read import (
            _execute_setup_statements,
            _read_dbapi_cursor,
        )

        time_print(
            f"Executing {max(len(statements) - 1, 0)} setup statement(s) and reading final query",
            backend=self.backend,
        )
        cursor = connection.cursor()
        try:
            _execute_setup_statements(
                cursor,
                statements[:-1],
                connection_type=self.backend,
                execute_statement=lambda cursor, statement: cursor.execute(statement),
                print_queries=print_queries,
                progress=progress,
            )
            return _read_dbapi_cursor(
                cursor,
                statements[-1],
                self.backend,
                print_queries,
            )
        except Exception:
            time_print(f"Failed SQL:\n{statements[-1]}", backend=self.backend)
            raise
        finally:
            cursor.close()

    def insert_dataframe_batch(
        self,
        connection: Any,
        table_name: str,
        batch: Any,
        *,
        target_column_types: dict[str, str] | None,
        trino_insert_chunk_size: int | None,
        gp_insert_chunk_size: int | None,
        connection_type: str,
        query_label: str | None,
        on_progress: Callable[[int], None] | None,
    ) -> None:
        del gp_insert_chunk_size

        self._insert_dataframe_batch(
            connection,
            table_name,
            batch,
            target_column_types=target_column_types,
            trino_insert_chunk_size=trino_insert_chunk_size,
            connection_type=connection_type,
            query_label=query_label,
            on_progress=on_progress,
        )

    def insert_rows_batch(
        self,
        connection: Any,
        table_name: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        target_column_types: dict[str, str] | None,
        trino_insert_chunk_size: int | None,
        gp_insert_chunk_size: int | None,
        connection_type: str,
        query_label: str | None,
        on_progress: Callable[[int], None] | None,
        gp_insert_page_size_getter: Callable[[], int] | None = None,
        on_gp_insert_page_success: Callable[[float, int], None] | None = None,
    ) -> None:
        del gp_insert_chunk_size, gp_insert_page_size_getter, on_gp_insert_page_success

        self._insert_rows(
            connection,
            table_name,
            columns,
            rows,
            target_column_types=target_column_types,
            trino_insert_chunk_size=trino_insert_chunk_size,
            connection_type=connection_type,
            query_label=query_label,
            on_progress=on_progress,
        )

    def _insert_dataframe_batch(
        self,
        connection: Any,
        table_name: str,
        batch: Any,
        *,
        target_column_types: dict[str, str] | None = None,
        trino_insert_chunk_size: int | None = None,
        connection_type: str = "trino",
        query_label: str | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        rows = list(batch.itertuples(index=False, name=None))
        self._insert_rows(
            connection,
            table_name,
            batch.columns,
            rows,
            target_column_types=target_column_types,
            trino_insert_chunk_size=trino_insert_chunk_size,
            connection_type=connection_type,
            query_label=query_label,
            on_progress=on_progress,
        )

    def _insert_rows(
        self,
        connection: Any,
        table_name: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        target_column_types: dict[str, str] | None = None,
        trino_insert_chunk_size: int | None = None,
        connection_type: str = "trino",
        query_label: str | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        _insert.insert_rows(
            self,
            connection,
            table_name,
            columns,
            rows,
            target_column_types=target_column_types,
            trino_insert_chunk_size=trino_insert_chunk_size,
            connection_type=connection_type,
            query_label=query_label,
            on_progress=on_progress,
        )

    def resolve_table_info_table_name(
        self,
        table_name: str,
        *,
        connection_key: str,
    ) -> str | None:
        from ...core.identifiers import (  # noqa: PLC0415, TID252 - registry cycle.
            resolve_trino_table_name,
        )

        return resolve_trino_table_name(table_name, connection_key=connection_key)

    def running_query_ids_sql(self) -> str:
        return """select query_id
from system.runtime.queries
where "user" = current_user
  and state in ('QUEUED', 'RUNNING')
  and query not like '%system.runtime.queries%'"""

    def show_queries_sqls(
        self,
        *,
        user: str | None,
        states: Sequence[str],
    ) -> list[dict[str, Any]]:
        queries: list[dict[str, Any]] = []
        user_filter = _user_filter('"user"', "current_user", user)
        if "active" in states:
            queries.append(
                {
                    "sql": f"""select
    query_id,
    "user",
    'active' as state,
    query,
    cast(null as timestamp) as started_at,
    cast(null as timestamp) as finished_at,
    cast(null as double) as elapsed_seconds,
    source,
    cast(null as varchar) as database,
    state as raw_state
from system.runtime.queries
where {user_filter}
  and state in ('QUEUED', 'RUNNING')
  and query not like '%system.runtime.queries%'""",
                    "history": False,
                }
            )

        history_states = [state for state in states if state != "active"]
        if history_states:
            state_filter = _sql_in_list(
                "state",
                [_trino_history_state(state) for state in history_states],
            )
            queries.append(
                {
                    "sql": f"""select
    query_id,
    "user",
    case
        when state = 'FINISHED' then 'finished'
        when state = 'FAILED' then 'failed'
        else lower(state)
    end as state,
    query,
    cast(null as timestamp) as started_at,
    cast(null as timestamp) as finished_at,
    cast(null as double) as elapsed_seconds,
    source,
    cast(null as varchar) as database,
    state as raw_state
from system.runtime.queries
where {user_filter}
  and {state_filter}
  and query not like '%system.runtime.queries%'""",
                    "history": True,
                }
            )
        return queries

    def cancel_query_sql(self, query_id: int | str) -> str:
        normalized_id = self.normalize_query_id(query_id)
        return (
            "CALL system.runtime.kill_query("
            f"query_id => {_sql_string_literal(str(normalized_id))}, "
            "message => 'Cancelled by analytics_toolkit.cancel_queries')"
        )

    def cancel_error_result(self, error: Exception) -> dict[str, Any] | None:
        if getattr(error, "error_name", None) == "NOT_SUPPORTED" and str(
            getattr(error, "message", "")
        ).startswith("Target query is not running:"):
            return {"cancelled": False, "terminated": None, "status": "not_running"}
        return None


def _trino_history_state(state: str) -> str:
    if state == "finished":
        return "FINISHED"
    if state == "failed":
        return "FAILED"
    raise ValueError(f"Unsupported Trino history state: {state}")


def split_trino_table_name(
    table_name: str,
    connection_key: str = "trino",
) -> tuple[str, str, str]:
    from ...core.identifiers import split_trino_table_name as _split_trino_table_name

    return _split_trino_table_name(table_name, connection_key=connection_key)


def _build_trino_table_properties(
    *,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
) -> str:
    properties = [
        "format = 'PARQUET'",
        "object_store_layout_enabled = true",
    ]
    partition_entries = _normalize_trino_property_entries(partition_by, "partition_by")
    if partition_entries:
        properties.append(f"partitioning = {_trino_string_array_sql(partition_entries)}")
    order_entries = _normalize_trino_property_entries(order_by, "order_by")
    if order_entries:
        properties.append(f"sorted_by = {_trino_string_array_sql(order_entries)}")
    return ", ".join(properties)


def _normalize_trino_property_entries(
    value: Sequence[str] | str | None,
    option_name: str,
) -> list[str]:
    from ..ch.ddl import _normalize_non_empty_string

    if value is None:
        return []
    if isinstance(value, str):
        return [_normalize_non_empty_string(value, option_name)]

    entries = [_normalize_non_empty_string(entry, option_name) for entry in value]
    if not entries:
        raise ValueError(f"{option_name} must not be empty when provided.")
    if len(set(entries)) != len(entries):
        raise ValueError(f"{option_name} must not contain duplicate entries.")
    return entries


def _trino_string_array_sql(entries: Sequence[str]) -> str:
    from ..ch.ddl import _sql_string_literal

    return "ARRAY[" + ", ".join(_sql_string_literal(entry) for entry in entries) + "]"


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _map_to_trino_type(
    kind: str,
    source_type: str,
    precision: int | None,
    scale: int | None,
) -> str:
    if kind == "binary":
        return "VARBINARY"
    if kind == "boolean":
        return "BOOLEAN"
    if kind == "integer":
        if "tiny" in source_type or source_type in {"int8", "uint8"}:
            return "TINYINT"
        if "small" in source_type or source_type in {"int16", "uint16"}:
            return "SMALLINT"
        if source_type in {"integer", "int", "int4", "int32", "uint32"}:
            return "INTEGER" if source_type != "uint32" else "BIGINT"
        if source_type == "uint64":
            return "DECIMAL(20, 0)"
        return "BIGINT"
    if kind == "float":
        if source_type in {"real", "float4", "float32"}:
            return "REAL"
        return "DOUBLE"
    if kind == "decimal":
        return _source_schema.decimal_type(
            "DECIMAL",
            precision,
            scale,
            fallback="DECIMAL(38, 10)",
            max_precision=_TRINO_MAX_DECIMAL_PRECISION,
        )
    if kind == "date":
        return "DATE"
    if kind == "timestamp":
        if "with time zone" in source_type or "timestamptz" in source_type:
            return "TIMESTAMP WITH TIME ZONE"
        return "TIMESTAMP"
    return "UUID" if kind == "uuid" else "VARCHAR"


def _validate_trino_partition_drop_template(template: str | None) -> str:
    if not template:
        raise ValueError(
            "Trino write_mode='upsert' requires "
            "upsert_partition_drop_sql_template in the target connection config."
        )
    allowed = {"table", "partition_column", "partition_value"}
    used: set[str] = set()
    for _, field_name, _, _ in Formatter().parse(template):
        if field_name is None:
            continue
        root_name = field_name.split(".", 1)[0].split("[", 1)[0]
        if root_name not in allowed:
            raise ValueError(
                "upsert_partition_drop_sql_template contains unsupported "
                f"placeholder {{{field_name}}}."
            )
        used.add(root_name)
    missing = allowed - used
    if missing:
        raise ValueError(
            "upsert_partition_drop_sql_template must contain placeholders: "
            + ", ".join(f"{{{name}}}" for name in sorted(allowed))
        )
    return template
