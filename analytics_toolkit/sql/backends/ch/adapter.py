from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ..base import BackendAdapter, BackendName, _apply_query_label


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
        raw_config["send_receive_timeout"] = 6000
        raw_config["settings"] = {"connect_timeout": "500"}
        copy_extra_fields(
            raw_config,
            extras,
            [
                "secure",
                "verify",
                "ca_certs",
                "transfer_staging_schema",
                "ca_certs_variable",
                "connect_timeout",
                "send_receive_timeout",
                "settings",
                "interface",
                "query_limit",
                "query_retries",
                "client_name",
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
        partition_by: Sequence[str] | str | None,
        order_by: Sequence[str] | str | None,
        ch_engine: str,
        ch_cluster: str,
        ch_sharding_key: str,
        ch_distributed_table: bool,
        ch_only_shard: bool,
        ch_replace_table: bool,
    ) -> list[str]:
        del gp_distributed_by_key
        from ...ddl.clickhouse import _build_ch_create_table_sqls

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
            _apply_query_label(
                f"TRUNCATE TABLE IF EXISTS {table_name}",
                query_label,
            )
        ]

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
        return None

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
        result = connection.query(
            self.count_table_rows_sql(table_name, query_label=query_label)
        )
        rows = getattr(result, "result_rows", None) or []
        return int(rows[0][0]) if rows else 0

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

    def inspect_source_query_schema(self, connection: Any, query: str) -> list[Any]:
        from ...dml.transfer.schema import _inspect_ch_source_schema

        return _inspect_ch_source_schema(connection, query)

    def map_source_type_to_target(self, column: Any) -> str:
        from ...dml.transfer import schema as transfer_schema

        source_type = transfer_schema._normalize_type_name(column.native_type)
        precision, scale = transfer_schema._type_precision_scale(column, source_type)
        kind = transfer_schema._classify_source_type(source_type)
        base_type = transfer_schema._map_to_ch_base_type(
            kind,
            source_type,
            precision,
            scale,
        )
        return transfer_schema._nullable_ch_type(base_type)

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
    ) -> list[str]:
        from ...clickhouse.lifecycle import ch_distributed_table_pair
        from ...dml.table import write_modes

        delete_table = (
            target_table
            if ch_only_shard
            else ch_distributed_table_pair(target_table).shard_table
        )
        return [
            _apply_query_label(
                write_modes._build_ch_delete_matching_stage_sql(
                    delete_table,
                    stage_table,
                    key_columns,
                    ch_cluster=None if ch_only_shard else ch_cluster,
                ),
                query_label,
            ),
            write_modes._build_insert_from_stage_sql(
                self.backend,
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
    ) -> list[str]:
        from ...clickhouse.lifecycle import ch_distributed_table_pair
        from ...dml.table import write_modes

        delete_table = (
            target_table
            if ch_only_shard
            else ch_distributed_table_pair(target_table).shard_table
        )
        return [
            _apply_query_label(
                write_modes._build_ch_delete_matching_stage_sql(
                    delete_table,
                    stage_table,
                    key_columns,
                    ch_cluster=None if ch_only_shard else ch_cluster,
                ),
                query_label,
            ),
            write_modes._build_insert_from_stage_placeholder_sql(
                self.backend,
                target_table,
                stage_table,
                query_label=query_label,
            ),
        ]

    def running_query_ids_sql(self) -> str:
        return """select query_id
from system.processes
where user = currentUser()
  and query_id != currentQueryID()"""

    def cancel_query_sql(self, query_id: int | str) -> str:
        normalized_id = self.normalize_query_id(query_id)
        return (
            "KILL QUERY "
            f"WHERE query_id = {_sql_string_literal(str(normalized_id))} SYNC"
        )

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
