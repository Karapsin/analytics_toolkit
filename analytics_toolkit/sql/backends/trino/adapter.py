from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ..base import _apply_query_label
from ..dbapi import DbApiBackendAdapter


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

    def __init__(self) -> None:
        super().__init__(backend="trino", commit_commands=False)

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
            [
                "catalog",
                "schema",
                "transfer_staging_schema",
                "transfer_staging_location",
                "auth_mode",
                "http_scheme",
                "verify",
                "ca_certs",
                "insert_chunk_size",
                "request_timeout",
                "source",
            ],
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
            gp_distributed_by_key,
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
        return [
            f"CREATE TABLE {table_name} ({joined_columns}) "
            f"WITH ({properties})"
        ]

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
            f"INSERT INTO {table_name} ({self.column_list_sql(columns)}) "
            f"VALUES {values_sql}",
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
                str(column_name): str(data_type)
                for column_name, data_type in cursor.fetchall()
            }
        finally:
            cursor.close()

    def running_query_ids_sql(self) -> str:
        return """select query_id
from system.runtime.queries
where "user" = current_user
  and state in ('QUEUED', 'RUNNING')
  and query not like '%system.runtime.queries%'"""

    def cancel_query_sql(self, query_id: int | str) -> str:
        normalized_id = self.normalize_query_id(query_id)
        return (
            "CALL system.runtime.kill_query("
            f"query_id => {_sql_string_literal(str(normalized_id))}, "
            "message => 'Cancelled by analytics_toolkit.cancel_queries')"
        )


def split_trino_table_name(
    table_name: str,
    connection_key: str = "trino",
) -> tuple[str, str, str]:
    from ...connection.config import TrinoConfig, get_connection_config

    parts = [part.strip() for part in table_name.split(".") if part.strip()]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]

    config = get_connection_config(connection_key)
    if not isinstance(config, TrinoConfig):
        raise ValueError("Invalid Trino configuration.")

    if len(parts) == 2:
        if not config.catalog:
            raise ValueError(
                f"Trino table operations for schema-qualified names require "
                f".connections['{config.connection_key}'].catalog."
            )
        return config.catalog, parts[0], parts[1]
    if len(parts) == 1:
        if not config.catalog or not config.schema:
            raise ValueError(
                f"Trino table operations for unqualified names require "
                f".connections['{config.connection_key}'].catalog and schema."
            )
        return config.catalog, config.schema, parts[0]
    raise ValueError(f"Invalid table name: {table_name}")


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
        properties.append(
            f"partitioning = {_trino_string_array_sql(partition_entries)}"
        )
    order_entries = _normalize_trino_property_entries(order_by, "order_by")
    if order_entries:
        properties.append(f"sorted_by = {_trino_string_array_sql(order_entries)}")
    return ", ".join(properties)


def _normalize_trino_property_entries(
    value: Sequence[str] | str | None,
    option_name: str,
) -> list[str]:
    from ...ddl.clickhouse import _normalize_non_empty_string

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
    from ...ddl.clickhouse import _sql_string_literal

    return "ARRAY[" + ", ".join(_sql_string_literal(entry) for entry in entries) + "]"


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
