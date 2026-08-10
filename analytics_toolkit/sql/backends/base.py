from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import adapter_defaults as _adapter_defaults
from . import common_methods as _common_methods
from . import source_count as _source_count
from . import upsert as _upsert
from . import validation as _validation
from .models import (
    BackendCapability,
    BackendName,
    SourceColumn,
    StageFinalizationRequest,
    StageTargetTableRequest,
    TargetWriteModeRequest,
    WriteMode,
)
from .utils import extract_row_count


class BackendAdapter:
    backend: BackendName
    display_name: str
    sqlglot_dialect: str
    identifier_quote: str
    supports_transactions: bool
    supports_analyze: bool
    uses_stage_tables: bool = True
    supports_distributed_tables: bool
    truncate_semantics: str
    drop_semantics: str
    create_semantics: str
    type_family: str
    supported_write_modes: frozenset[WriteMode] = frozenset(
        {"append", "replace", "truncate_insert", "upsert"}
    )
    supports_early_transfer_target_creation: bool = True
    upsert_strategy: str = "key_delete_insert"
    requires_upsert_partition_column: bool = False
    requires_upsert_partition_drop_template: bool = False
    supports_show_tables_catalog_filter: bool = False
    forbidden_airflow_file_override_fields: frozenset[str] = frozenset()

    @property
    def name(self) -> BackendName:
        return self.backend

    @property
    def capability(self) -> BackendCapability:
        return BackendCapability(
            name=self.backend,
            display_name=self.display_name,
            sqlglot_dialect=self.sqlglot_dialect,
            identifier_quote=self.identifier_quote,
            supports_transactions=self.supports_transactions,
            supports_analyze=self.supports_analyze,
            uses_stage_tables=self.uses_stage_tables,
            supports_distributed_tables=self.supports_distributed_tables,
            truncate_semantics=self.truncate_semantics,
            drop_semantics=self.drop_semantics,
            create_semantics=self.create_semantics,
            type_family=self.type_family,
            supported_write_modes=self.supported_write_modes,
            supports_early_transfer_target_creation=(self.supports_early_transfer_target_creation),
            upsert_strategy=self.upsert_strategy,
            requires_upsert_partition_column=self.requires_upsert_partition_column,
            requires_upsert_partition_drop_template=(self.requires_upsert_partition_drop_template),
            supports_show_tables_catalog_filter=self.supports_show_tables_catalog_filter,
        )

    def build_connection_config(self, connection_key: str, raw_config: dict[str, Any]) -> Any:
        raise NotImplementedError

    def copy_airflow_fields(
        self,
        raw_config: dict[str, Any],
        extras: dict[str, Any],
        connection: Any,
        copy_extra_fields: Callable[[dict[str, Any], dict[str, Any], Sequence[str]], None],
        set_if_not_none: Callable[[dict[str, Any], str, Any], None],
    ) -> None:
        raise NotImplementedError

    validate_airflow_file_overrides = _adapter_defaults.validate_airflow_file_overrides

    def open_connection(
        self,
        config: Any,
        *,
        parse_verify_value: Callable[[str], bool | str],
        resolve_ca_certs: Callable[[str, list[str]], str | None],
        resolve_single_cert_path: Callable[[str, str, str], Any],
        resolve_ch_ca_certs: Callable[[Any], str | None],
    ) -> Any:
        raise NotImplementedError

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
    ) -> list[str]:
        raise NotImplementedError

    def execute_command(self, connection: Any, sql: str) -> Any:
        raise NotImplementedError

    execute_commands = _common_methods.execute_commands
    read_dataframe = _common_methods.read_dataframe
    _read_dataframe_impl = _common_methods.read_dataframe_impl

    def table_exists(
        self,
        connection: Any,
        table_name: str,
        *,
        connection_key: str,
    ) -> bool:
        raise NotImplementedError

    def clear_table_sqls(
        self,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def clear_table(
        self,
        connection: Any,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> None:
        self.execute_commands(
            connection,
            self.clear_table_sqls(table_name, query_label=query_label),
        )

    def drop_table_sql(
        self,
        table_name: str,
        *,
        if_exists: bool = True,
        ch_cluster: str | None = None,
        query_label: str | None = None,
    ) -> str:
        del ch_cluster
        prefix = "DROP TABLE IF EXISTS" if if_exists else "DROP TABLE"
        return _apply_query_label(f"{prefix} {table_name}", query_label)

    drop_table = _adapter_defaults.drop_table

    build_creation_policy_cleanup_sqls = (
        _adapter_defaults.build_creation_policy_cleanup_sqls
    )
    preclear_distributed_replace_target = (
        _adapter_defaults.preclear_distributed_replace_target
    )
    open_transfer_host_connection = _adapter_defaults.open_transfer_host_connection
    needs_bounded_replace_preclear = _adapter_defaults.needs_bounded_replace_preclear

    def analyze_table_sql(
        self,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> str:
        return _apply_query_label(f"ANALYZE {table_name}", query_label)

    should_analyze_table = _adapter_defaults.should_analyze_table

    def analyze_table(
        self,
        connection: Any,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> None:
        self.execute_command(
            connection,
            self.analyze_table_sql(table_name, query_label=query_label),
        )

    validate_write_mode = _adapter_defaults.validate_write_mode
    normalize_ch_columns_or_expression = _adapter_defaults.normalize_ch_columns_or_expression
    normalize_ch_string = _adapter_defaults.normalize_ch_string
    validate_ch_columns_in_columns = _adapter_defaults.validate_ch_columns_in_columns

    def count_table_rows_sql(
        self,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> str:
        return _apply_query_label(f"SELECT COUNT(*) FROM {table_name}", query_label)

    def count_table_rows(
        self,
        connection: Any,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> int:
        cursor = connection.cursor()
        try:
            cursor.execute(self.count_table_rows_sql(table_name, query_label=query_label))
            row = cursor.fetchone()
            return int(row[0]) if row else 0
        finally:
            cursor.close()

    build_source_count_sql = _source_count.build_source_count_sql
    count_source_rows = _source_count.count_source_rows
    source_sql_for_count_limited_read = _source_count.source_sql_for_count_limited_read
    disable_query_limit_for_transfer_reads = _source_count.disable_query_limit_for_transfer_reads
    strip_query_semicolon = _source_count.strip_query_semicolon

    def get_table_column_types(
        self,
        connection: Any,
        table_name: str,
        *,
        connection_key: str,
    ) -> dict[str, str]:
        raise NotImplementedError

    def inspect_source_query_schema(
        self,
        connection: Any,
        query: str,
    ) -> list[SourceColumn]:
        raise NotImplementedError

    def map_source_type_to_target(self, column: SourceColumn) -> str:
        raise NotImplementedError
    map_same_backend_source_type_to_target = _adapter_defaults.map_same_backend_source_type_to_target
    refine_stage_column_types_from_rows = _adapter_defaults.refine_stage_column_types_from_rows

    build_show_tables_query = _adapter_defaults.build_show_tables_query
    postprocess_show_tables = _adapter_defaults.postprocess_show_tables
    allows_show_tables_catalog_filter = _adapter_defaults.allows_show_tables_catalog_filter
    extract_table_ddl = _adapter_defaults.extract_table_ddl
    validate_drop_partitions_options = _adapter_defaults.validate_drop_partitions_options
    build_drop_partitions_sqls = _adapter_defaults.build_drop_partitions_sqls
    build_create_partition_sql = _adapter_defaults.build_create_partition_sql
    query_transfer_stage_table_names = _adapter_defaults.query_transfer_stage_table_names
    qualify_transfer_stage_table_name = _adapter_defaults.qualify_transfer_stage_table_name
    stage_base_identifier = _adapter_defaults.stage_base_identifier
    build_drop_tables_sqls = _adapter_defaults.build_drop_tables_sqls
    build_drop_target_sqls = _adapter_defaults.build_drop_target_sqls
    drop_table_with_options = _adapter_defaults.drop_table_with_options
    build_clear_target_sqls = _adapter_defaults.build_clear_target_sqls
    build_transfer_replace_target_sqls = _adapter_defaults.build_transfer_replace_target_sqls
    transfer_replace_target_phase = _adapter_defaults.transfer_replace_target_phase
    transfer_replace_existing_non_ch = _adapter_defaults.transfer_replace_existing_non_ch
    companion_table_name = _adapter_defaults.companion_table_name
    target_connection_defaults = _adapter_defaults.target_connection_defaults
    validate_ch_create_table_options = _adapter_defaults.validate_ch_create_table_options
    resolve_table_info_table_name = _adapter_defaults.resolve_table_info_table_name
    rollback_quietly = _adapter_defaults.rollback_quietly
    wait_for_table_absence = _adapter_defaults.wait_for_table_absence
    build_vacuum_table_sql = _adapter_defaults.build_vacuum_table_sql
    vacuum_table = _adapter_defaults.vacuum_table
    prepare_existing_target_for_create_from_sql = (
        _adapter_defaults.prepare_existing_target_for_create_from_sql
    )
    estimate_source_rows = _adapter_defaults.estimate_source_rows
    after_create_table = _adapter_defaults.after_create_table
    expected_create_table_column_types = _adapter_defaults.expected_create_table_column_types

    def iter_source_batches(
        self,
        *,
        connection_key: str,
        connection_ref: dict[str, Any],
        query: str,
        get_batch_size: Callable[[], int],
        retry_cnt: int,
        timeout_increment: float,
        disable_query_limit: bool = False,
    ) -> Any:
        del (
            connection_key,
            connection_ref,
            query,
            get_batch_size,
            retry_cnt,
            timeout_increment,
            disable_query_limit,
        )
        raise NotImplementedError

    def build_upsert_stage_sqls(
        self,
        target_table: str,
        stage_table: str,
        *,
        columns: Sequence[str],
        key_columns: Sequence[str],
        column_types: Mapping[str, str] | None = None,
        ch_cluster: str = "{cluster}",
        ch_only_shard: bool = False,
        query_label: str | None = None,
        upsert_partition_column: str | None = None,
        final_stage_table: str | None = None,
        incoming_stage_tables: Sequence[str] | None = None,
        partition_values: Sequence[Any] | None = None,
        trino_partition_drop_sql_template: str | None = None,
    ) -> list[str]:
        raise NotImplementedError

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
        raise NotImplementedError

    build_upsert_partition_values_sql = _upsert.build_upsert_partition_values_sql
    fetch_upsert_partition_values = _upsert.fetch_upsert_partition_values
    build_partition_replacement_upsert_sqls = _upsert.build_partition_replacement_upsert_sqls
    build_preserved_target_rows_insert_sql = _upsert.build_preserved_target_rows_insert_sql
    build_incoming_rows_insert_sql = _upsert.build_incoming_rows_insert_sql

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
        del (
            target_table,
            partition_column,
            partition_values,
            query_label,
            trino_partition_drop_sql_template,
            ch_cluster,
            ch_only_shard,
        )
        raise NotImplementedError

    _incoming_stage_source_sql = _upsert.incoming_stage_source_sql

    def build_insert_from_stage_sql(
        self,
        target_table: str,
        stage_table: str,
        *,
        columns: Sequence[str],
        column_types: Mapping[str, str] | None,
        query_label: str | None,
    ) -> str:
        typed_columns = self.column_types_for_columns(column_types, columns)
        return _apply_query_label(
            self.build_explicit_insert_from_stage_sql(
                target_table,
                stage_table,
                columns=columns,
                column_types=typed_columns,
            ),
            query_label,
        )

    def build_explicit_insert_from_stage_sql(
        self,
        target_table: str,
        stage_table: str,
        *,
        columns: Sequence[str],
        column_types: Mapping[str, str] | None,
    ) -> str:
        if column_types:
            return self.build_insert_from_table_sql(
                target_table,
                stage_table,
                column_types,
            )

        target_columns = self.column_list_sql(columns)
        selected_columns = ", ".join(self.quote_identifier(column) for column in columns)
        return (
            f"INSERT INTO {target_table} ({target_columns}) "
            f"SELECT {selected_columns} FROM {stage_table}"
        )

    def build_insert_from_stage_placeholder_sql(
        self,
        target_table: str,
        stage_table: str,
        *,
        query_label: str | None,
    ) -> str:
        return _apply_query_label(
            f"INSERT INTO {target_table} (<source query columns>) "
            f"SELECT <source query columns> FROM {stage_table}",
            query_label,
        )

    def column_types_for_columns(
        self,
        column_types: Mapping[str, str] | None,
        columns: Sequence[str],
    ) -> dict[str, str] | None:
        if column_types is None:
            return None

        missing_columns = [column for column in columns if column not in column_types]
        if missing_columns:
            raise ValueError(
                "Target table is missing staged column(s): " + ", ".join(missing_columns)
            )
        return {column: column_types[column] for column in columns}

    should_ensure_load_target_table = _adapter_defaults.should_ensure_load_target_table
    build_load_target_create_kwargs = _adapter_defaults.build_load_target_create_kwargs
    build_create_from_sql_target_create_kwargs = (
        _adapter_defaults.build_create_from_sql_target_create_kwargs
    )

    def planned_execute_statements(
        self,
        sql: str,
        *,
        gp_break_query: bool = False,
    ) -> list[str]:
        del gp_break_query
        from ..dml.io.execute_sql import _split_sql_statements

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
        raise NotImplementedError

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
        raise NotImplementedError

    normalize_insert_batch = _adapter_defaults.normalize_insert_batch
    normalize_transfer_source_batch = _adapter_defaults.normalize_transfer_source_batch
    mark_upsert_finalization_error = _adapter_defaults.mark_upsert_finalization_error
    normalize_insert_rows = _adapter_defaults.normalize_insert_rows
    should_wrap_insert_error_as_ambiguous = _adapter_defaults.should_wrap_insert_error_as_ambiguous
    should_refresh_connection_before_insert_retry = (
        _adapter_defaults.should_refresh_connection_before_insert_retry
    )
    transfer_attempt_policy = _adapter_defaults.transfer_attempt_policy
    transfer_insert_page_sizing = _adapter_defaults.transfer_insert_page_sizing
    requires_load_target_column_metadata = _adapter_defaults.requires_load_target_column_metadata
    uses_partition_replacement_upsert = _adapter_defaults.uses_partition_replacement_upsert
    needs_upsert_partition_drop_template = _adapter_defaults.needs_upsert_partition_drop_template
    supports_distributed_table_targets = _adapter_defaults.supports_distributed_table_targets
    can_create_transfer_target_before_batches = (
        _adapter_defaults.can_create_transfer_target_before_batches
    )
    resolve_ch_retry_per_host_drops = _adapter_defaults.resolve_ch_retry_per_host_drops
    validate_gp_distributed_by_key_option = _adapter_defaults.validate_gp_distributed_by_key_option
    normalize_gp_partitions_option = _adapter_defaults.normalize_gp_partitions_option
    validate_gp_insert_chunk_size_option = _adapter_defaults.validate_gp_insert_chunk_size_option
    validate_trino_insert_chunk_size_option = (
        _adapter_defaults.validate_trino_insert_chunk_size_option
    )
    resolve_transfer_staging_mode = _adapter_defaults.resolve_transfer_staging_mode
    create_table_from_sql_fast_path = _adapter_defaults.create_table_from_sql_fast_path
    uses_create_table_from_sql_fast_path = _adapter_defaults.uses_create_table_from_sql_fast_path
    should_insert_create_table_from_sql_directly = (
        _adapter_defaults.should_insert_create_table_from_sql_directly
    )
    resolve_transfer_stage_column_types = _adapter_defaults.resolve_transfer_stage_column_types

    def insert_dataframe_batch(
        self,
        connection: Any,
        table_name: str,
        batch: Any,
        *,
        target_column_types: Mapping[str, str] | None,
        trino_insert_chunk_size: int | None,
        gp_insert_chunk_size: int | None,
        connection_type: str,
        query_label: str | None,
        on_progress: Callable[[int], None] | None,
    ) -> None:
        raise NotImplementedError

    def insert_rows_batch(
        self,
        connection: Any,
        table_name: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        target_column_types: Mapping[str, str] | None,
        trino_insert_chunk_size: int | None,
        gp_insert_chunk_size: int | None,
        connection_type: str,
        query_label: str | None,
        on_progress: Callable[[int], None] | None,
        gp_insert_page_size_getter: Callable[[], int] | None = None,
        on_gp_insert_page_success: Callable[[float, int], None] | None = None,
    ) -> None:
        raise NotImplementedError

    def apply_target_write_mode(self, request: TargetWriteModeRequest) -> bool:
        from analytics_toolkit.general import time_print

        if request.write_mode == "append":
            return request.target_exists
        if not request.target_exists:
            return False
        if request.write_mode == "truncate_insert" or request.replace_existing_non_ch == "clear":
            self.clear_table(
                request.connection,
                request.table_name,
                query_label=request.query_label,
            )
            return True
        if request.replace_existing_non_ch == "drop":
            time_print(
                f"Dropping existing table {request.table_name}",
                connection=request.connection_label or self.backend,
                backend=self.backend,
            )
            self.drop_table(
                request.connection,
                request.table_name,
                query_label=request.query_label,
            )
            return False
        raise ValueError("replace_existing_non_ch must be one of: clear, drop.")

    def ensure_stage_target_table(self, request: StageTargetTableRequest) -> bool:
        from ..ddl.api import _create_sql_table_with_connection

        create_kwargs: dict[str, Any] = {}
        if request.partition_by is not None:
            create_kwargs["partition_by"] = request.partition_by
        if request.gp_partitions is not None:
            create_kwargs["gp_partitions"] = request.gp_partitions
        if request.order_by is not None:
            create_kwargs["order_by"] = request.order_by
        _create_sql_table_with_connection(
            self.backend,
            request.connection,
            request.target_table,
            None if request.target_column_types is not None else request.sample_batch,
            connection_key=request.connection_key or self.backend,
            table_schema=request.target_column_types,
            gp_distributed_by_key=request.gp_distributed_by_key,
            query_label=request.query_label,
            **create_kwargs,
        )
        return True

    def finalize_stage_table(self, request: StageFinalizationRequest) -> None:
        target_exists = request.target_exists
        if request.write_mode == "upsert":
            if not target_exists:
                self.ensure_stage_target_table(
                    StageTargetTableRequest(
                        connection=request.connection,
                        target_table=request.target_table,
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
                self.insert_from_table(
                    request.connection,
                    request.target_table,
                    request.stage_table,
                    column_types=request.insert_column_types,
                    query_label=request.query_label,
                )
                return

            partition_values = None
            if request.upsert_partition_column is not None:
                partition_values = self.fetch_upsert_partition_values(
                    request.connection,
                    request.stage_table,
                    partition_column=request.upsert_partition_column,
                    incoming_stage_tables=request.incoming_stage_tables,
                )

            for sql in self.build_upsert_stage_sqls(
                request.target_table,
                request.stage_table,
                columns=list(
                    request.insert_column_types
                    or request.target_column_types
                    or request.sample_batch.columns
                ),
                key_columns=request.key_columns or [],
                column_types=request.insert_column_types,
                ch_cluster=request.ch_cluster,
                ch_only_shard=request.ch_only_shard,
                query_label=request.query_label,
                upsert_partition_column=request.upsert_partition_column,
                final_stage_table=request.final_upsert_stage_table,
                incoming_stage_tables=request.incoming_stage_tables,
                partition_values=partition_values,
                trino_partition_drop_sql_template=(
                    request.trino_upsert_partition_drop_sql_template
                ),
            ):
                self.execute_command(request.connection, sql)
            return

        if request.replace_target_table:
            target_exists = self.apply_target_write_mode(
                TargetWriteModeRequest(
                    connection=request.connection,
                    table_name=request.target_table,
                    write_mode=request.write_mode,
                    target_exists=target_exists,
                    replace_existing_non_ch=self.transfer_replace_existing_non_ch(),
                    ch_cluster=request.ch_cluster,
                    query_label=request.query_label,
                    connection_key=request.connection_key,
                    ch_retry_per_host_drops=request.ch_retry_per_host_drops,
                    ch_only_shard=request.ch_only_shard,
                )
            )

        if not target_exists:
            self.ensure_stage_target_table(
                StageTargetTableRequest(
                    connection=request.connection,
                    target_table=request.target_table,
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

        self.insert_from_table(
            request.connection,
            request.target_table,
            request.stage_table,
            column_types=request.insert_column_types,
            query_label=request.query_label,
        )

    def type_code_name(
        self,
        type_code: Any,
        precision: int | None,
        scale: int | None,
    ) -> str | None:
        del precision, scale
        if type_code is None:
            return None
        for attribute in ("name", "type_name", "typename"):
            value = getattr(type_code, attribute, None)
            if value:
                return str(value)
        return str(type_code)

    def running_query_ids_sql(self) -> str:
        raise NotImplementedError

    def show_queries_sqls(
        self,
        *,
        user: str | None,
        states: Sequence[str],
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def normalize_query_id(self, query_id: Any) -> int | str:
        if isinstance(query_id, str):
            normalized = query_id.strip()
            if not normalized:
                raise ValueError("query_ids must not contain empty strings.")
            return normalized
        if isinstance(query_id, int) and not isinstance(query_id, bool):
            return str(query_id)
        raise ValueError("query_ids must contain strings or integers.")

    def cancel_query_sql(self, query_id: int | str) -> str:
        raise NotImplementedError

    def cancel_status(self, result: Any) -> tuple[bool, str]:
        return True, "submitted"

    def cancel_result(self, result: Any) -> dict[str, Any]:
        cancelled, status = self.cancel_status(result)
        return {
            "cancelled": cancelled,
            "terminated": None,
            "status": status,
        }

    build_stage_duplicate_keys_sql = _validation.build_stage_duplicate_keys_sql
    build_stage_duplicate_keys_sql_for_tables = (
        _validation.build_stage_duplicate_keys_sql_for_tables
    )
    build_stage_target_key_overlap_sql = _validation.build_stage_target_key_overlap_sql
    stage_has_duplicate_keys = _validation.stage_has_duplicate_keys
    stage_keys_overlap_target = _validation.stage_keys_overlap_target
    query_has_rows = _validation.query_has_rows

    def build_insert_from_table_sql(
        self,
        target_table: str,
        source_table: str,
        column_types: Mapping[str, str] | None = None,
    ) -> str:
        if not column_types:
            return f"INSERT INTO {target_table} SELECT * FROM {source_table}"

        return self._build_typed_insert_select_sql(
            target_table,
            f"FROM {source_table}",
            column_types,
        )

    def build_insert_from_query_sql(
        self,
        target_table: str,
        source_sql: str,
        column_types: Mapping[str, str],
    ) -> str:
        query_sql = source_sql.strip()
        if query_sql.endswith(";"):
            query_sql = query_sql[:-1].strip()
        return self._build_typed_insert_select_sql(
            target_table,
            f"FROM ({query_sql}) AS source_query",
            column_types,
        )

    def build_dataframe_batch_insert_sql(
        self,
        table_name: str,
        columns: Sequence[str],
        *,
        row_count: int,
        query_label: str | None = None,
    ) -> str:
        del table_name, columns, row_count, query_label
        from ..connection.errors import UnsupportedConnectionTypeError

        raise UnsupportedConnectionTypeError(
            f"{self.backend} does not support SQL VALUES dataframe batch inserts."
        )

    def insert_from_table(
        self,
        connection: Any,
        target_table: str,
        source_table: str,
        *,
        column_types: Mapping[str, str] | None = None,
        query_label: str | None = None,
    ) -> None:
        self.execute_command(
            connection,
            _apply_query_label(
                self.build_insert_from_table_sql(
                    target_table,
                    source_table,
                    column_types,
                ),
                query_label,
            ),
        )

    def insert_from_query(
        self,
        connection: Any,
        target_table: str,
        source_sql: str,
        column_types: Mapping[str, str],
        *,
        query_label: str | None = None,
    ) -> int:
        executed = self.execute_command(
            connection,
            _apply_query_label(
                self.build_insert_from_query_sql(
                    target_table,
                    source_sql,
                    column_types,
                ),
                query_label,
            ),
        )
        return extract_row_count(executed)

    def _build_typed_insert_select_sql(
        self,
        target_table: str,
        from_sql: str,
        column_types: Mapping[str, str],
    ) -> str:
        columns = list(column_types)
        target_columns = self.column_list_sql(columns)
        select_columns = ", ".join(
            self.cast_select_expression(column_name, target_type)
            for column_name, target_type in column_types.items()
        )
        return f"INSERT INTO {target_table} ({target_columns}) SELECT {select_columns} {from_sql}"

    def column_list_sql(self, columns: Sequence[str]) -> str:
        return ", ".join(self.quote_identifier(column_name) for column_name in columns)

    def cast_select_expression(self, column_name: str, target_type: str) -> str:
        quoted_column = self.quote_identifier(column_name)
        return f"CAST({quoted_column} AS {target_type}) AS {quoted_column}"

    def null_safe_key_equality(
        self,
        left_alias: str,
        right_alias: str,
        column_name: str,
    ) -> str:
        quoted_column = self.quote_identifier(column_name)
        left_expr = f"{left_alias}.{quoted_column}"
        right_expr = f"{right_alias}.{quoted_column}"
        return f"({left_expr} = {right_expr} OR ({left_expr} IS NULL AND {right_expr} IS NULL))"

    def quote_identifier(self, identifier: str) -> str:
        escaped = identifier.replace(self.identifier_quote, self.identifier_quote * 2)
        return f"{self.identifier_quote}{escaped}{self.identifier_quote}"


def _apply_query_label(sql: str, query_label: str | None) -> str:
    from ..execution.labels import apply_query_label

    return apply_query_label(sql, query_label)
