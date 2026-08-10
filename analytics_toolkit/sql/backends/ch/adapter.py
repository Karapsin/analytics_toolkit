from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

from .. import dataframe_types as _dataframe_types
from ..base import (
    BackendAdapter,
    BackendName,
    StageFinalizationRequest,
    StageTargetTableRequest,
    TargetWriteModeRequest,
    _apply_query_label,
)
from . import create_table_from_sql as _create_from_sql
from . import insert as _insert
from . import operations as _operations
from . import queries as _queries
from . import reconfigure_proxy as _reconfigure
from . import source_count as _source_count
from . import source_schema as _ch_source_schema
from . import target_create as _target_create
from . import transfer_cleanup as _cleanup
from . import upsert as _upsert
from .config import AIRFLOW_EXTRA_FIELDS

ON_CLUSTER_COMMAND_SETTINGS = {
    "distributed_ddl_task_timeout": 0,
    "distributed_ddl_output_mode": "none",
}


class ClickHouseAdapter(BackendAdapter):
    backend: BackendName = "ch"
    display_name = "ClickHouse"
    sqlglot_dialect = "clickhouse"
    identifier_quote = "`"
    supports_transactions = False
    supports_analyze = False
    supports_distributed_tables = True
    truncate_semantics = "TRUNCATE TABLE IF EXISTS"
    drop_semantics = "DROP TABLE IF EXISTS plus distributed pair when requested"
    create_semantics = "MergeTree or shard plus Distributed pair"
    type_family = "clickhouse"
    supports_early_transfer_target_creation = False
    upsert_strategy = "partition_replace"
    requires_upsert_partition_column = True
    supports_ch_create_table_options = True
    resolve_ch_retry_per_host_drops = staticmethod(bool)
    create_table_from_sql_fast_path = _create_from_sql.create_table_from_sql_fast_path
    uses_create_table_from_sql_fast_path = _create_from_sql.uses_create_table_from_sql_fast_path
    read_columns = _operations.read_columns
    _read_columns_impl = _operations.read_columns_impl

    def build_connection_config(self, connection_key: str, raw_config: dict[str, Any]) -> Any:
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
        raw_config["send_receive_timeout"] = 6000
        raw_config["settings"] = {"connect_timeout": "500"}
        copy_extra_fields(raw_config, extras, AIRFLOW_EXTRA_FIELDS)

    def open_connection(
        self,
        config: Any,
        *,
        parse_verify_value: Callable[[str], bool | str],
        resolve_ca_certs: Callable[[str, list[str]], str | None],
        resolve_single_cert_path: Callable[[str, str, str], Any],
        resolve_ch_ca_certs: Callable[[Any], str | None],
    ) -> Any:
        del resolve_ca_certs, resolve_single_cert_path
        from .config import open_connection

        return open_connection(
            config,
            parse_verify_value=parse_verify_value,
            resolve_ch_ca_certs=resolve_ch_ca_certs,
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
    ) -> list[str]:
        del gp_distributed_by_key, gp_partitions
        from .ddl import _build_ch_create_table_sqls

        return _build_ch_create_table_sqls(
            table_name=table_name,
            joined_columns=joined_columns,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_distributed_table=ch_distributed_table,
            ch_only_shard=ch_only_shard,
            ch_replace_table=ch_replace_table,
        )

    def execute_command(self, connection: Any, sql: str) -> Any:
        if "ON CLUSTER" not in sql:
            return connection.command(sql)

        try:
            return connection.command(
                sql,
                settings=ON_CLUSTER_COMMAND_SETTINGS,
            )
        except TypeError:
            return connection.command(sql)

    plan_table_reconfiguration = _reconfigure.plan
    execute_table_reconfiguration = _reconfigure.execute

    def _read_dataframe_impl(
        self,
        connection: Any,
        query: str,
        read_dbapi_query: Callable[[Any, str], Any],
    ) -> Any:
        del read_dbapi_query
        return connection.query_df(query)

    def table_exists(
        self,
        connection: Any,
        table_name: str,
        *,
        connection_key: str,
    ) -> bool:
        del connection_key
        result = connection.query(f"EXISTS TABLE {table_name}")
        return bool(result.result_rows and result.result_rows[0][0])

    def clear_table_sqls(
        self,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> list[str]:
        return [
            self.build_truncate_table_sql(
                table_name,
                query_label=query_label,
            )
        ]

    def build_truncate_table_sql(
        self,
        table_name: str,
        *,
        ch_cluster: str | None = None,
        query_label: str | None = None,
    ) -> str:
        from .lifecycle import _build_truncate_ch_table_sql

        return _build_truncate_ch_table_sql(
            table_name,
            ch_cluster=ch_cluster,
            query_label=query_label,
        )

    def truncate_table(
        self,
        connection: Any,
        table_name: str,
        *,
        ch_cluster: str | None = None,
        query_label: str | None = None,
    ) -> None:
        self.execute_command(
            connection,
            self.build_truncate_table_sql(
                table_name,
                ch_cluster=ch_cluster,
                query_label=query_label,
            ),
        )

    build_show_tables_query = _operations.build_show_tables_query
    postprocess_show_tables = _operations.postprocess_show_tables
    extract_table_ddl = _operations.extract_table_ddl
    build_drop_partitions_sqls = _operations.build_drop_partitions_sqls
    query_transfer_stage_table_names = _operations.query_transfer_stage_table_names
    qualify_transfer_stage_table_name = _operations.qualify_transfer_stage_table_name
    build_drop_tables_sqls = _operations.build_drop_tables_sqls
    drop_table_with_options = _operations.drop_table_with_options
    build_clear_target_sqls = _operations.build_clear_target_sqls
    build_transfer_replace_target_sqls = _operations.build_transfer_replace_target_sqls
    transfer_replace_target_phase = _operations.transfer_replace_target_phase
    companion_table_name = _operations.companion_table_name
    build_drop_target_sqls = _operations.build_drop_target_sqls
    prepare_existing_target_for_create_from_sql = (
        _operations.prepare_existing_target_for_create_from_sql
    )
    wait_for_table_absence = _operations.wait_for_table_absence
    estimate_source_rows = _operations.estimate_source_rows
    infer_dataframe_column_type = _dataframe_types.infer_ch_dataframe_column_type

    def after_create_table(
        self,
        connection: Any,
        table_name: str,
        *,
        ch_cluster: str = "{cluster}",
        ch_distributed_table: bool = False,
        ch_only_shard: bool = False,
        expected_column_types: dict[str, str] | None = None,
        ch_creation_policy: Any = None,
    ) -> None:
        from .wait import after_create_table

        after_create_table(
            self,
            connection,
            table_name,
            ch_cluster=ch_cluster,
            ch_distributed_table=ch_distributed_table,
            ch_only_shard=ch_only_shard,
            expected_column_types=expected_column_types,
            ch_creation_policy=ch_creation_policy,
        )

    def drop_table_sql(
        self,
        table_name: str,
        *,
        if_exists: bool = True,
        ch_cluster: str | None = None,
        query_label: str | None = None,
    ) -> str:
        prefix = "DROP TABLE IF EXISTS" if if_exists else "DROP TABLE"
        return _apply_query_label(
            f"{prefix} {table_name}{ch_cluster_clause(ch_cluster)}",
            query_label,
        )

    build_creation_policy_cleanup_sqls = _cleanup.build_creation_policy_cleanup_sqls
    preclear_distributed_replace_target = _cleanup.preclear_distributed_replace_target
    open_transfer_host_connection = _cleanup.open_transfer_host_connection
    needs_bounded_replace_preclear = _cleanup.needs_bounded_replace_preclear

    def analyze_table_sql(
        self,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> str:
        del table_name, query_label
        from ...connection.errors import UnsupportedConnectionTypeError

        raise UnsupportedConnectionTypeError("ClickHouse does not support ANALYZE here.")

    def analyze_table(
        self,
        connection: Any,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> None:
        del connection, table_name, query_label

    def count_table_rows_sql(
        self,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> str:
        return _apply_query_label(f"SELECT count() FROM {table_name}", query_label)

    def count_table_rows(
        self,
        connection: Any,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> int:
        result = connection.query(self.count_table_rows_sql(table_name, query_label=query_label))
        rows = getattr(result, "result_rows", None) or []
        return int(rows[0][0]) if rows else 0

    count_source_rows = _source_count.count_source_rows
    source_sql_for_count_limited_read = _source_count.source_sql_for_count_limited_read
    disable_query_limit_for_transfer_reads = _source_count.disable_query_limit_for_transfer_reads
    build_materialize_transfer_source_sql = _operations.build_materialize_transfer_source_sql

    def get_table_column_types(
        self,
        connection: Any,
        table_name: str,
        *,
        connection_key: str,
    ) -> dict[str, str]:
        del connection_key
        result = connection.query(f"DESCRIBE TABLE {table_name}")
        rows = getattr(result, "result_rows", None) or []
        return {str(row[0]): str(row[1]) for row in rows if len(row) >= 2}

    def query_has_rows(self, connection: Any, sql: str) -> bool:
        result = connection.query(sql)
        return bool(getattr(result, "result_rows", None) or [])

    inspect_source_query_schema = _ch_source_schema.inspect_source_query_schema
    map_source_type_to_target = _ch_source_schema.map_source_type_to_target
    refine_stage_column_types_from_rows = _ch_source_schema.refine_stage_column_types_from_rows
    normalize_transfer_source_batch = _ch_source_schema.normalize_transfer_source_batch
    mark_upsert_finalization_error = _ch_source_schema.mark_upsert_finalization_error

    build_upsert_stage_sqls = _upsert.build_upsert_stage_sqls
    build_preserved_target_rows_insert_sql = _upsert.build_preserved_target_rows_insert_sql
    build_upsert_stage_placeholder_sqls = _upsert.build_upsert_stage_placeholder_sqls
    build_drop_upsert_partition_sqls = _upsert.build_drop_upsert_partition_sqls

    def _build_delete_matching_stage_sql(
        self,
        target_table: str,
        stage_table: str,
        key_columns: Sequence[str],
        *,
        ch_cluster: str | None,
    ) -> str:
        target_tuple = self._build_normalized_key_tuple(key_columns)
        stage_tuple = self._build_normalized_key_tuple(key_columns)
        return (
            f"DELETE FROM {target_table}{ch_cluster_clause(ch_cluster)}\n"
            f"WHERE {target_tuple} IN (\n"
            f"  SELECT {stage_tuple} FROM {stage_table}\n"
            ")"
        )

    def _build_normalized_key_tuple(self, key_columns: Sequence[str]) -> str:
        expressions: list[str] = []
        for column_name in key_columns:
            quoted_column = self.quote_identifier(column_name)
            expressions.extend(
                [
                    f"isNull({quoted_column})",
                    f"ifNull(toString({quoted_column}), '')",
                ]
            )
        return "tuple(" + ", ".join(expressions) + ")"

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

        from ...dml.io.execute_sql import (
            _iterate_statements_with_progress,
            _maybe_print_query,
            _split_sql_statements,
        )
        from ...execution.query_timing import run_timed_query

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
                    lambda statement=statement: connection.command(statement),
                )
        except Exception:
            failed_query = statement if statement is not None else sql
            time_print(f"Failed SQL:\n{failed_query}", backend=self.backend)
            raise
        return None

    def fetch_upsert_partition_values(
        self,
        connection: Any,
        stage_table: str,
        *,
        partition_column: str,
        incoming_stage_tables: Sequence[str] | None = None,
    ) -> list[Any]:
        result = connection.query(
            self.build_upsert_partition_values_sql(
                stage_table,
                partition_column=partition_column,
                incoming_stage_tables=incoming_stage_tables,
            )
        )
        rows = getattr(result, "result_rows", None) or []
        return [row[0] for row in rows]

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

        from ...dml.io.execute_read import _execute_setup_statements
        from ...dml.io.execute_sql import _maybe_print_query
        from ...execution.query_timing import run_timed_query

        time_print(
            f"Executing {max(len(statements) - 1, 0)} setup statement(s) and reading final query",
            backend=self.backend,
        )
        try:
            _execute_setup_statements(
                connection,
                statements[:-1],
                connection_type=self.backend,
                execute_statement=lambda client, statement: client.command(statement),
                print_queries=print_queries,
                progress=progress,
            )
            _maybe_print_query(statements[-1], print_queries, split_preview=True)
            return run_timed_query(
                self.backend,
                lambda: connection.query_df(statements[-1]),
                phase="read",
            )
        except Exception:
            time_print(f"Failed SQL:\n{statements[-1]}", backend=self.backend)
            raise

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
    ) -> Iterator[Any]:
        from ...dml.transfer.io.source import _iter_clickhouse_batches

        yield from _iter_clickhouse_batches(
            connection_key,
            connection_ref,
            query,
            get_batch_size,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            disable_query_limit=disable_query_limit,
        )

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
        del target_column_types, trino_insert_chunk_size, gp_insert_chunk_size
        del connection_type, query_label

        self._insert_dataframe_batch(
            connection,
            table_name,
            batch,
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
        del trino_insert_chunk_size, gp_insert_chunk_size, connection_type, query_label
        del gp_insert_page_size_getter, on_gp_insert_page_success

        self._insert_rows(
            connection,
            table_name,
            columns,
            rows,
            target_column_types,
            on_progress=on_progress,
        )

    def _insert_dataframe_batch(
        self,
        connection: Any,
        table_name: str,
        batch: Any,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        normalized_batch = _insert.normalize_batch(batch)
        connection.insert_df(
            table=table_name,
            df=normalized_batch,
            column_names=list(batch.columns),
        )
        if on_progress is not None:
            on_progress(len(batch))

    def _insert_rows(
        self,
        connection: Any,
        table_name: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        column_types: dict[str, str] | None,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        connection.insert(
            table=table_name,
            data=[_insert.normalize_row(row) for row in rows],
            column_names=list(columns),
            column_type_names=_insert.column_type_names(columns, column_types),
        )
        if on_progress is not None:
            on_progress(len(rows))

    def apply_target_write_mode(self, request: TargetWriteModeRequest) -> bool:
        from analytics_toolkit.general import time_print

        from ...connection.get_sql_connection import get_ch_connection_for_host
        from .lifecycle import (
            drop_ch_distributed_table_pair,
            truncate_ch_distributed_table_pair,
        )

        if request.write_mode == "append":
            return request.target_exists

        if request.ch_only_shard:
            if request.write_mode == "truncate_insert" and request.target_exists:
                self.clear_table(
                    request.connection,
                    request.table_name,
                    query_label=request.query_label,
                )
                return True
            if (
                request.write_mode == "truncate_insert"
                and not request.drop_missing_ch_truncate_target
            ):
                return False

            time_print(f"Dropping existing ClickHouse table {request.table_name}")
            self.drop_table(
                request.connection,
                request.table_name,
                ch_cluster=None,
                query_label=request.query_label,
            )
            return False

        if request.write_mode == "truncate_insert" and request.target_exists:
            truncate_ch_distributed_table_pair(
                request.connection,
                request.table_name,
                ch_cluster=request.ch_cluster,
                query_label=request.query_label,
            )
            return True
        if request.write_mode == "truncate_insert" and not request.drop_missing_ch_truncate_target:
            return False

        time_print(f"Dropping existing ClickHouse distributed table pair {request.table_name}")
        per_host_connection_factory = (
            (lambda host: get_ch_connection_for_host(request.connection_key, host))
            if request.connection_key is not None
            else None
        )
        drop_ch_distributed_table_pair(
            request.connection,
            request.table_name,
            ch_cluster=request.ch_cluster,
            query_label=request.query_label,
            wait_for_absence=True,
            ch_retry_per_host_drops=request.ch_retry_per_host_drops,
            per_host_connection_factory=per_host_connection_factory,
        )
        return False

    def ensure_stage_target_table(self, request: StageTargetTableRequest) -> bool:
        self.ensure_distributed_target_pair(
            request.connection,
            request.target_table,
            request.sample_batch,
            target_exists=False,
            target_column_types=request.target_column_types,
            insert_column_types=request.target_column_types,
            gp_distributed_by_key=request.gp_distributed_by_key,
            partition_by=request.partition_by,
            order_by=request.order_by,
            ch_engine=request.ch_engine,
            ch_cluster=request.ch_cluster,
            ch_sharding_key=request.ch_sharding_key,
            query_label=request.query_label,
            connection_key=request.connection_key,
            ch_replace_table=False,
            ch_only_shard=request.ch_only_shard,
            ch_creation_policy=request.ch_creation_policy,
        )
        return True

    def finalize_stage_table(self, request: StageFinalizationRequest) -> None:
        target_exists = request.target_exists
        original_target_exists = target_exists
        if request.write_mode == "upsert":
            if not target_exists:
                self.ensure_stage_target_table(
                    StageTargetTableRequest(
                        connection=request.connection,
                        target_table=request.target_table,
                        sample_batch=request.sample_batch,
                        target_column_types=request.target_column_types,
                        gp_distributed_by_key=request.gp_distributed_by_key,
                        partition_by=request.partition_by,
                        order_by=request.order_by,
                        ch_engine=request.ch_engine,
                        ch_cluster=request.ch_cluster,
                        ch_sharding_key=request.ch_sharding_key,
                        query_label=request.query_label,
                        connection_key=request.connection_key,
                        ch_only_shard=request.ch_only_shard,
                        ch_creation_policy=request.ch_creation_policy,
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

            self.ensure_distributed_target_pair(
                request.connection,
                request.target_table,
                request.sample_batch,
                target_exists=target_exists,
                target_column_types=request.target_column_types,
                insert_column_types=request.insert_column_types,
                gp_distributed_by_key=request.gp_distributed_by_key,
                partition_by=request.partition_by,
                order_by=request.order_by,
                ch_engine=request.ch_engine,
                ch_cluster=request.ch_cluster,
                ch_sharding_key=request.ch_sharding_key,
                query_label=request.query_label,
                connection_key=request.connection_key,
                ch_replace_table=False,
                ch_only_shard=request.ch_only_shard,
                ch_creation_policy=request.ch_creation_policy,
            )
            if request.upsert_partition_column is None:
                raise ValueError(
                    "upsert_partition_column is required for ClickHouse write_mode='upsert'."
                )
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
                    replace_existing_non_ch="clear",
                    ch_cluster=request.ch_cluster,
                    query_label=request.query_label,
                    connection_key=request.connection_key,
                    ch_retry_per_host_drops=request.ch_retry_per_host_drops,
                    ch_only_shard=request.ch_only_shard,
                )
            )

        self.ensure_distributed_target_pair(
            request.connection,
            request.target_table,
            request.sample_batch,
            target_exists=target_exists,
            target_column_types=request.target_column_types,
            insert_column_types=request.insert_column_types,
            gp_distributed_by_key=request.gp_distributed_by_key,
            partition_by=request.partition_by,
            order_by=request.order_by,
            ch_engine=request.ch_engine,
            ch_cluster=request.ch_cluster,
            ch_sharding_key=request.ch_sharding_key,
            query_label=request.query_label,
            connection_key=request.connection_key,
            ch_replace_table=(
                original_target_exists
                and request.replace_target_table
                and request.write_mode == "replace"
                and not request.ch_only_shard
            ),
            ch_only_shard=request.ch_only_shard,
            ch_creation_policy=request.ch_creation_policy,
        )
        self.insert_from_table(
            request.connection,
            request.target_table,
            request.stage_table,
            column_types=request.insert_column_types,
            query_label=request.query_label,
        )

    def ensure_distributed_target_pair(
        self,
        connection: Any,
        target_table: str,
        sample_batch: Any,
        *,
        target_exists: bool,
        target_column_types: dict[str, str] | None,
        insert_column_types: dict[str, str] | None,
        gp_distributed_by_key: list[str] | None,
        partition_by: list[str] | str | None,
        order_by: list[str] | str | None,
        ch_engine: str,
        ch_cluster: str,
        ch_sharding_key: str,
        query_label: str | None,
        connection_key: str | None,
        ch_replace_table: bool = False,
        ch_only_shard: bool = False,
        ch_creation_policy: Any = None,
    ) -> None:
        import pandas as pd

        from ...ddl.api import _create_sql_table_with_connection

        create_batch = sample_batch
        create_column_types = target_column_types or insert_column_types
        if target_exists:
            existing_column_types = self.get_table_column_types(
                connection,
                target_table,
                connection_key=connection_key or self.backend,
            )
            if existing_column_types:
                create_batch = pd.DataFrame(columns=list(existing_column_types))
                create_column_types = existing_column_types

        _create_sql_table_with_connection(
            self.backend,
            connection,
            target_table,
            None if create_column_types is not None else create_batch,
            connection_key=connection_key or self.backend,
            table_schema=create_column_types,
            gp_distributed_by_key=gp_distributed_by_key,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_distributed_table=not ch_only_shard,
            ch_only_shard=ch_only_shard,
            ch_replace_table=ch_replace_table,
            query_label=query_label,
            ch_creation_policy=ch_creation_policy,
        )

    should_ensure_load_target_table = _target_create.should_ensure_load_target_table
    build_load_target_create_kwargs = _target_create.build_load_target_create_kwargs
    build_create_from_sql_target_create_kwargs = (
        _target_create.build_create_from_sql_target_create_kwargs
    )
    expected_create_table_column_types = _target_create.expected_create_table_column_types

    def running_query_ids_sql(self) -> str:
        return """select query_id
from system.processes
where user = currentUser()
  and query_id != currentQueryID()"""

    show_queries_sqls = _queries.show_queries_sqls

    def cancel_query_sql(self, query_id: int | str) -> str:
        normalized_id = self.normalize_query_id(query_id)
        return f"KILL QUERY WHERE query_id = {_sql_string_literal(str(normalized_id))} SYNC"

    def cancel_status(self, result: Any) -> tuple[bool, str]:
        if "kill_status" in result.columns and not result.empty:
            statuses = [str(value) for value in result["kill_status"].tolist()]
            return all(status == "finished" for status in statuses), ", ".join(statuses)
        return True, "submitted"


def ch_cluster_clause(ch_cluster: str | None) -> str:
    if ch_cluster is None:
        return ""
    normalized = ch_cluster.strip()
    if not normalized:
        raise ValueError("ch_cluster must not be empty.")
    return f" ON CLUSTER {format_ch_cluster_name(normalized)}"


def format_ch_cluster_name(cluster_name: str) -> str:
    if cluster_name[0] in {"'", '"', "`"}:
        return cluster_name
    if is_simple_identifier(cluster_name):
        return cluster_name
    return "'" + cluster_name.replace("'", "''") + "'"


def is_simple_identifier(identifier: str) -> bool:
    if not identifier:
        return False
    if not (identifier[0].isalpha() or identifier[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in identifier)


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
