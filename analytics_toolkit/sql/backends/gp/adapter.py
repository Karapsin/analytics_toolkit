from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ..base import _apply_query_label
from ..dbapi import DbApiBackendAdapter


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

    def running_query_ids_sql(self) -> str:
        return """select pid as query_id
from pg_stat_activity
where usename = current_user
  and pid <> pg_backend_pid()"""

    def normalize_query_id(self, query_id: Any) -> int | str:
        if isinstance(query_id, bool):
            raise ValueError("Greenplum query_ids must be backend PIDs.")
        try:
            return int(query_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Greenplum query_ids must be backend PIDs.") from exc

    def cancel_query_sql(self, query_id: int | str) -> str:
        normalized_id = self.normalize_query_id(query_id)
        return f"select pg_cancel_backend({normalized_id}) as cancelled"

    def cancel_status(self, result: Any) -> tuple[bool, str]:
        cancelled = bool(result["cancelled"].iloc[0])
        return cancelled, "cancelled" if cancelled else "not_cancelled"


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
    from ...ddl.clickhouse import _normalize_non_empty_string

    if isinstance(partition_by, str):
        return _normalize_non_empty_string(partition_by, "partition_by")

    columns = [
        _normalize_non_empty_string(column, "partition_by")
        for column in partition_by
    ]
    if len(columns) != 1:
        raise ValueError("partition_by for Greenplum must contain exactly one column.")
    return columns[0]
