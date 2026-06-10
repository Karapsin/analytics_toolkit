from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..connection.config import BackendName
from ..connection.errors import UnsupportedConnectionTypeError
from ..execution.labels import apply_query_label
from .base import BackendAdapter


ON_CLUSTER_COMMAND_SETTINGS = {
    "distributed_ddl_task_timeout": 0,
    "distributed_ddl_output_mode": "none",
}


class ClickHouseAdapter(BackendAdapter):
    backend: BackendName = "ch"

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
            apply_query_label(
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
        return apply_query_label(
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
        return apply_query_label(f"SELECT count() FROM {table_name}", query_label)

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
