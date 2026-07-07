from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from . import operations as _operations
from . import insert as _insert
from . import stage as _stage
from .. import dataframe_types as _dataframe_types
from .. import source_schema as _source_schema
from ..base import _apply_query_label
from ..models import SourceColumn
from ..models import TransferAttemptPolicy
from ..models import TransferInsertPageSizing
from ..utils import user_filter as _user_filter
from ..dbapi import DbApiBackendAdapter


_GP_OID_TYPES = {
    16: "boolean",
    17: "bytea",
    20: "bigint",
    21: "smallint",
    23: "integer",
    25: "text",
    700: "real",
    701: "double precision",
    1042: "character",
    1043: "character varying",
    1082: "date",
    1083: "time",
    1114: "timestamp",
    1184: "timestamp with time zone",
    1700: "numeric",
    2950: "uuid",
    3802: "jsonb",
}
_GP_MAX_NUMERIC_PRECISION = 1000
GP_IDENTIFIER_MAX_BYTES = _stage.GP_IDENTIFIER_MAX_BYTES


class GreenplumAdapter(DbApiBackendAdapter):
    display_name = "Greenplum"
    sqlglot_dialect = "postgres"
    identifier_quote = '"'
    supports_transactions = True
    supports_analyze = True
    supports_distributed_tables = False
    truncate_semantics = "TRUNCATE TABLE"
    drop_semantics = "DROP TABLE IF EXISTS"
    create_semantics = "CREATE TABLE with append-only columnar storage"
    type_family = "postgres"
    supports_create_table_order_by = False

    def __init__(self) -> None:
        super().__init__(backend="gp", commit_commands=True)

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
        set_if_not_none(raw_config, "database", getattr(connection, "schema", None))
        copy_extra_fields(
            raw_config,
            extras,
            [
                "connect_timeout",
                "keepalives",
                "keepalives_idle",
                "keepalives_interval",
                "keepalives_count",
                "sslmode",
                "transfer_staging_schema",
                "ca_certs",
                "ssl_cert",
                "ssl_key",
            ],
        )

    def open_connection(
        self,
        config: Any,
        *,
        parse_verify_value: Callable[[str], bool | str],
        resolve_ca_certs: Callable[[str, list[str]], str | None],
        resolve_single_cert_path: Callable[[str, str, str], Any],
        resolve_ch_ca_certs: Callable[[Any], str | None],
    ) -> Any:
        del parse_verify_value, resolve_ch_ca_certs
        from .config import open_connection

        return open_connection(
            config,
            resolve_ca_certs=resolve_ca_certs,
            resolve_single_cert_path=resolve_single_cert_path,
        )

    def build_create_table_sqls(
        self,
        *,
        table_name: str,
        joined_columns: str,
        gp_distributed_by_key: list[str] | None,
        partition_by: Sequence[str] | str | None,
        order_by: Sequence[str] | str | None,
        ch_engine: str,
        ch_cluster: str,
        ch_sharding_key: str,
        ch_distributed_table: bool,
        ch_only_shard: bool,
        ch_replace_table: bool,
    ) -> list[str]:
        del (
            ch_engine,
            ch_cluster,
            ch_sharding_key,
            ch_distributed_table,
            ch_only_shard,
            ch_replace_table,
        )
        if order_by is not None:
            raise ValueError("order_by is not supported for Greenplum create table.")
        storage_sql = (
            "WITH (appendonly=true,\n"
            "        blocksize=32768,\n"
            "        compresstype=zstd,\n"
            "        compresslevel=4,\n"
            "        orientation=column)"
        )
        if gp_distributed_by_key:
            distribution_sql = (
                f"DISTRIBUTED BY ({self.column_list_sql(gp_distributed_by_key)})"
            )
        else:
            distribution_sql = "DISTRIBUTED RANDOMLY"
        partition_sql = _build_gp_partition_by_sql(partition_by)
        return [
            f"CREATE TABLE {table_name} ({joined_columns}) "
            f"{storage_sql} {distribution_sql}{partition_sql}"
        ]

    def table_exists(
        self,
        connection: Any,
        table_name: str,
        *,
        connection_key: str,
    ) -> bool:
        del connection_key
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT to_regclass(%s)", (table_name,))
            row = cursor.fetchone()
            return bool(row and row[0])
        finally:
            cursor.close()

    def clear_table_sqls(
        self,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> list[str]:
        return [_apply_query_label(f"TRUNCATE TABLE {table_name}", query_label)]

    build_show_tables_query = _operations.build_show_tables_query
    extract_table_ddl = _operations.extract_table_ddl
    validate_drop_partitions_options = _operations.validate_drop_partitions_options
    build_drop_partitions_sqls = _operations.build_drop_partitions_sqls
    build_create_partition_sql = _operations.build_create_partition_sql
    infer_dataframe_column_type = _dataframe_types.infer_gp_dataframe_column_type
    stage_base_identifier = _stage.stage_base_identifier

    def rollback_quietly(self, connection: Any) -> None:
        try:
            connection.rollback()
        except Exception:
            return None

    def build_vacuum_table_sql(
        self,
        table_name: str,
        *,
        analyze: bool = False,
        full: bool = False,
        verbose: bool = True,
    ) -> str:
        from ...dml.table._basic_ops import quote_qualified_table_name

        qualified_table_name = quote_qualified_table_name(table_name, self.backend)
        options: list[str] = []
        if full:
            options.append("FULL")
        if verbose:
            options.append("VERBOSE")
        if analyze:
            options.append("ANALYZE")
        options_sql = f" ({', '.join(options)})" if options else ""
        return f"VACUUM{options_sql} {qualified_table_name}"

    def estimate_source_rows(
        self,
        connection: Any,
        source_sql: str,
        *,
        query_label: str | None = None,
    ) -> int | None:
        from ..source_estimate import _estimate_gp_source_rows

        return _estimate_gp_source_rows(
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
        del row_count
        return _apply_query_label(
            f"INSERT INTO {table_name} ({self.column_list_sql(columns)}) VALUES %s",
            query_label,
        )

    def get_table_column_types(
        self,
        connection: Any,
        table_name: str,
        *,
        connection_key: str,
    ) -> dict[str, str]:
        del connection_key
        schema_name, relation_name = split_gp_table_name(table_name)
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT column_name, data_type, udt_name, numeric_precision, numeric_scale
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                ORDER BY ordinal_position
                """.strip(),
                (schema_name, relation_name),
            )
            return {
                str(column_name): format_gp_information_schema_type(
                    str(data_type),
                    udt_name,
                    numeric_precision,
                    numeric_scale,
                )
                for (
                    column_name,
                    data_type,
                    udt_name,
                    numeric_precision,
                    numeric_scale,
                ) in cursor.fetchall()
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
        return _map_to_gp_type(kind, source_type, precision, scale)

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
        del (
            ch_cluster,
            ch_only_shard,
            upsert_partition_column,
            final_stage_table,
            incoming_stage_tables,
            partition_values,
            trino_partition_drop_sql_template,
        )

        return [
            _apply_query_label(
                self._build_delete_matching_stage_sql(
                    target_table,
                    stage_table,
                    key_columns,
                ),
                query_label,
            ),
            self.build_insert_from_stage_sql(
                target_table,
                stage_table,
                columns=columns,
                column_types=column_types,
                query_label=query_label,
            ),
        ]

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
        del (
            ch_cluster,
            ch_only_shard,
            upsert_partition_column,
            final_stage_table,
            incoming_stage_tables,
            partition_values,
            trino_partition_drop_sql_template,
        )

        return [
            _apply_query_label(
                self._build_delete_matching_stage_sql(
                    target_table,
                    stage_table,
                    key_columns,
                ),
                query_label,
            ),
            self.build_insert_from_stage_placeholder_sql(
                target_table,
                stage_table,
                query_label=query_label,
            ),
        ]

    def _build_delete_matching_stage_sql(
        self,
        target_table: str,
        stage_table: str,
        key_columns: Sequence[str],
    ) -> str:
        predicates = " AND ".join(
            self.null_safe_key_equality("target_dst", "stage_src", column_name)
            for column_name in key_columns
        )
        return (
            f"DELETE FROM {target_table} AS target_dst\n"
            f"USING {stage_table} AS stage_src\n"
            f"WHERE {predicates}"
        )

    def planned_execute_statements(
        self,
        sql: str,
        *,
        gp_break_query: bool = False,
    ) -> list[str]:
        if not gp_break_query:
            return [sql]
        from ...dml.io.execute_sql import _split_sql_statements

        return _split_sql_statements(sql)

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
        from analytics_toolkit.general import time_print
        from ...execution.query_timing import run_timed_query
        from ...dml.io.execute_sql import (
            _iterate_statements_with_progress,
            _maybe_print_query,
            _split_sql_statements,
        )

        statement: str | None = None
        try:
            with connection.cursor() as cursor:
                should_commit_at_end = True
                if not gp_break_query:
                    time_print("Executing 1 statement set", backend=self.backend)
                    statement = sql
                    _maybe_print_query(statement, print_queries, split_preview=False)
                    run_timed_query(
                        self.backend,
                        lambda: cursor.execute(statement),
                    )
                else:
                    statements = _split_sql_statements(sql)
                    time_print(
                        f"Executing {len(statements)} statement(s)",
                        backend=self.backend,
                    )
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
                        if gp_commit_each_statement:
                            connection.commit()
                            should_commit_at_end = False
                if should_commit_at_end:
                    connection.commit()
                return None
        except Exception:
            failed_query = statement if statement is not None else sql
            time_print(f"Failed SQL:\n{failed_query}", backend=self.backend)
            raise

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
        from analytics_toolkit.general import time_print
        from ...execution.query_timing import run_timed_query
        from ...dml.io.execute_read import _read_dbapi_cursor
        from ...dml.io.execute_sql import (
            _iterate_statements_with_progress,
            _maybe_print_query,
        )

        time_print(
            f"Executing {max(len(statements) - 1, 0)} setup statement(s) "
            "and reading final query",
            backend=self.backend,
        )
        cursor = connection.cursor()
        should_commit_at_end = len(statements) > 1
        try:
            setup_statements = statements[:-1]
            if setup_statements and not gp_break_query:
                setup_sql = ";\n".join(setup_statements)
                _maybe_print_query(setup_sql, print_queries, split_preview=False)
                run_timed_query(self.backend, lambda: cursor.execute(setup_sql))
            else:
                for statement in _iterate_statements_with_progress(
                    setup_statements,
                    self.backend,
                    progress=progress,
                ):
                    _maybe_print_query(statement, print_queries, split_preview=True)
                    run_timed_query(
                        self.backend,
                        lambda statement=statement: cursor.execute(statement),
                    )
                    if gp_commit_each_statement:
                        connection.commit()
                        should_commit_at_end = False

            result = _read_dbapi_cursor(
                cursor,
                statements[-1],
                self.backend,
                print_queries,
            )
            if should_commit_at_end:
                connection.commit()
            return result
        except Exception:
            time_print(f"Failed SQL:\n{statements[-1]}", backend=self.backend)
            raise
        finally:
            cursor.close()

    def normalize_insert_batch(self, batch: Any) -> Any:
        return _insert.normalize_insert_batch(self, batch)

    def normalize_insert_rows(
        self,
        rows: Sequence[Sequence[Any]],
    ) -> list[tuple[Any, ...]]:
        return _insert.normalize_insert_rows(self, rows)

    def should_wrap_insert_error_as_ambiguous(
        self,
        connection: Any,
        exc: Exception,
    ) -> bool:
        del connection, exc
        return False

    def should_refresh_connection_before_insert_retry(self) -> bool:
        return True

    def transfer_attempt_policy(self, retry_cnt: int) -> TransferAttemptPolicy:
        return TransferAttemptPolicy(
            insert_retry_cnt=retry_cnt,
            retry_ambiguous_stage_load=False,
        )

    def transfer_insert_page_sizing(
        self,
        *,
        gp_insert_chunk_size: int | None,
    ) -> TransferInsertPageSizing:
        initial_size = gp_insert_chunk_size or _insert.DEFAULT_GP_INSERT_CHUNK_SIZE
        return TransferInsertPageSizing(
            initial_size=initial_size,
            min_size=min(1_000, initial_size),
            max_size=max(100_000, initial_size * 4),
        )

    def validate_gp_distributed_by_key_option(
        self,
        value: Sequence[str] | None,
        *,
        option_owner: str,
    ) -> None:
        del value, option_owner

    def validate_gp_insert_chunk_size_option(
        self,
        value: int | None,
        *,
        option_owner: str,
    ) -> None:
        del option_owner
        if value is not None and value <= 0:
            raise ValueError("gp_insert_chunk_size must be a positive integer.")

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
        del target_column_types, trino_insert_chunk_size, connection_type

        self._insert_dataframe_batch(
            connection,
            table_name,
            batch,
            gp_insert_chunk_size=gp_insert_chunk_size,
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
        del target_column_types, trino_insert_chunk_size, connection_type

        self._insert_rows(
            connection,
            table_name,
            columns,
            rows,
            gp_insert_chunk_size=gp_insert_chunk_size,
            query_label=query_label,
            on_progress=on_progress,
            page_size_getter=gp_insert_page_size_getter,
            on_page_success=on_gp_insert_page_success,
        )

    def _insert_dataframe_batch(
        self,
        connection: Any,
        table_name: str,
        batch: Any,
        *,
        gp_insert_chunk_size: int | None = None,
        query_label: str | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        rows = list(batch.itertuples(index=False, name=None))
        self._insert_rows(
            connection,
            table_name,
            batch.columns,
            rows,
            gp_insert_chunk_size=gp_insert_chunk_size,
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
        gp_insert_chunk_size: int | None = None,
        query_label: str | None = None,
        on_progress: Callable[[int], None] | None = None,
        page_size_getter: Callable[[], int] | None = None,
        on_page_success: Callable[[float, int], None] | None = None,
    ) -> None:
        _insert.insert_rows(
            self,
            connection,
            table_name,
            columns,
            rows,
            gp_insert_chunk_size=gp_insert_chunk_size,
            query_label=query_label,
            on_progress=on_progress,
            page_size_getter=page_size_getter,
            on_page_success=on_page_success,
        )

    def type_code_name(
        self,
        type_code: Any,
        precision: int | None,
        scale: int | None,
    ) -> str | None:
        if type_code is None:
            return None
        if isinstance(type_code, int):
            base_type = _GP_OID_TYPES.get(type_code, str(type_code))
            if base_type == "numeric" and precision is not None and scale is not None:
                return f"numeric({precision},{scale})"
            return base_type
        return super().type_code_name(type_code, precision, scale)

    def running_query_ids_sql(self) -> str:
        return """select pid as query_id
from pg_stat_activity
where usename = current_user
  and pid <> pg_backend_pid()"""

    def show_queries_sqls(
        self,
        *,
        user: str | None,
        states: Sequence[str],
    ) -> list[dict[str, Any]]:
        queries: list[dict[str, Any]] = []
        if "active" in states:
            user_filter = _user_filter("usename", "current_user", user)
            queries.append(
                {
                    "sql": f"""select
    pid as query_id,
    usename as "user",
    'active' as state,
    query,
    query_start as started_at,
    null::timestamp as finished_at,
    extract(epoch from (now() - query_start))::double precision as elapsed_seconds,
    application_name as source,
    datname as database,
    state as raw_state
from pg_stat_activity
where {user_filter}
  and pid <> pg_backend_pid()
  and state = 'active'""",
                    "history": False,
                }
            )
        unsupported_states = [state for state in states if state != "active"]
        if unsupported_states:
            queries.append(
                {
                    "sql": "",
                    "history": True,
                    "unsupported_states": tuple(unsupported_states),
                }
            )
        return queries

    def normalize_query_id(self, query_id: Any) -> int | str:
        if isinstance(query_id, bool):
            raise ValueError("Greenplum query_ids must be backend PIDs.")
        try:
            return int(query_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Greenplum query_ids must be backend PIDs.") from exc

    def cancel_query_sql(self, query_id: int | str) -> str:
        normalized_id = self.normalize_query_id(query_id)
        return f"""with cancel_attempt as (
    select pg_cancel_backend({normalized_id}) as cancelled
)
select cancelled, pg_terminate_backend({normalized_id}) as terminated
from cancel_attempt"""

    def cancel_status(self, result: Any) -> tuple[bool, str]:
        cancelled = bool(result["cancelled"].iloc[0])
        return cancelled, "cancelled" if cancelled else "not_cancelled"

    def cancel_result(self, result: Any) -> dict[str, Any]:
        cancelled = bool(result["cancelled"].iloc[0])
        terminated = bool(result["terminated"].iloc[0])
        status = (
            ("cancelled" if cancelled else "not_cancelled")
            + "_"
            + ("terminated" if terminated else "not_terminated")
        )
        return {
            "cancelled": cancelled,
            "terminated": terminated,
            "status": status,
        }


def split_gp_table_name(table_name: str) -> tuple[str, str]:
    parts = [part.strip().strip('"') for part in table_name.split(".") if part.strip()]
    if len(parts) == 1:
        return "public", parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError(f"Invalid Greenplum table name: {table_name}")


def format_gp_information_schema_type(
    data_type: str,
    udt_name: Any,
    numeric_precision: Any,
    numeric_scale: Any,
) -> str:
    normalized = data_type.lower()
    if normalized == "numeric" and numeric_precision is not None:
        if numeric_scale is None:
            return f"NUMERIC({numeric_precision})"
        return f"NUMERIC({numeric_precision}, {numeric_scale})"
    if normalized == "character varying":
        return "VARCHAR"
    if normalized == "timestamp without time zone":
        return "TIMESTAMP"
    if normalized == "timestamp with time zone":
        return "TIMESTAMP WITH TIME ZONE"
    if normalized == "integer":
        return "INTEGER"
    if normalized == "bigint":
        return "BIGINT"
    if normalized == "smallint":
        return "SMALLINT"
    if normalized == "boolean":
        return "BOOLEAN"
    if normalized == "date":
        return "DATE"
    if normalized == "text":
        return "TEXT"
    return str(udt_name or data_type).upper()


def _build_gp_partition_by_sql(partition_by: Sequence[str] | str | None) -> str:
    if partition_by is None:
        return ""
    partition_column = _normalize_gp_partition_column(partition_by)
    from ...ddl.identifiers import quote_identifier

    return f" PARTITION BY RANGE ({quote_identifier(partition_column, 'gp')})"


def _normalize_gp_partition_column(partition_by: Sequence[str] | str) -> str:
    from ..ch.ddl import _normalize_non_empty_string

    if isinstance(partition_by, str):
        return _normalize_non_empty_string(partition_by, "partition_by")

    columns = [
        _normalize_non_empty_string(column, "partition_by")
        for column in partition_by
    ]
    if len(columns) != 1:
        raise ValueError("partition_by for Greenplum must contain exactly one column.")
    return columns[0]


def _map_to_gp_type(
    kind: str,
    source_type: str,
    precision: int | None,
    scale: int | None,
) -> str:
    if kind == "binary":
        return "BYTEA"
    if kind == "boolean":
        return "BOOLEAN"
    if kind == "integer":
        if "small" in source_type or source_type in {"int16", "uint8"}:
            return "SMALLINT"
        if source_type in {"integer", "int", "int4", "int32", "uint16"}:
            return "INTEGER"
        if source_type in {"uint32"}:
            return "BIGINT"
        if source_type in {"uint64"}:
            return "NUMERIC(20, 0)"
        return "BIGINT"
    if kind == "float":
        if source_type in {"real", "float4", "float32"}:
            return "REAL"
        return "DOUBLE PRECISION"
    if kind == "decimal":
        return _source_schema.decimal_type(
            "NUMERIC",
            precision,
            scale,
            fallback="NUMERIC",
            max_precision=_GP_MAX_NUMERIC_PRECISION,
        )
    if kind == "date":
        return "DATE"
    if kind == "timestamp":
        if "with time zone" in source_type or "timestamptz" in source_type:
            return "TIMESTAMP WITH TIME ZONE"
        return "TIMESTAMP"
    return "TEXT"
