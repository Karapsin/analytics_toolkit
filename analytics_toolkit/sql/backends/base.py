from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .utils import extract_row_count


WriteMode = Literal["append", "replace", "truncate_insert", "upsert"]
BackendName = Literal["gp", "trino", "ch"]

UNSUPPORTED_BACKEND_MESSAGE = (
    "Unsupported connection type. Expected one of: 'trino', 'gp', 'ch'."
)


@dataclass(frozen=True)
class BackendCapability:
    name: BackendName
    display_name: str
    sqlglot_dialect: str
    identifier_quote: str
    supports_transactions: bool
    supports_analyze: bool
    uses_stage_tables: bool
    supports_distributed_tables: bool
    truncate_semantics: str
    drop_semantics: str
    create_semantics: str
    type_family: str
    supported_write_modes: frozenset[WriteMode]


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
        )

    def build_connection_config(
        self,
        connection_key: str,
        raw_config: dict[str, Any],
    ) -> Any:
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

    def execute_commands(self, connection: Any, sqls: list[str]) -> None:
        for sql in sqls:
            self.execute_command(connection, sql)

    def read_dataframe(
        self,
        connection: Any,
        query: str,
        *,
        print_queries: bool,
        print_query: Callable[[str, bool], None],
        read_dbapi_query: Callable[[Any, str], Any],
    ) -> Any:
        from analytics_toolkit.general import time_print

        time_print("Reading DataFrame", backend=self.backend)
        try:
            print_query(query, print_queries)
            return self._read_dataframe_impl(connection, query, read_dbapi_query)
        except Exception:
            time_print(f"Failed SQL:\n{query}", backend=self.backend)
            raise

    def _read_dataframe_impl(
        self,
        connection: Any,
        query: str,
        read_dbapi_query: Callable[[Any, str], Any],
    ) -> Any:
        return read_dbapi_query(connection, query)

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

    def drop_table(
        self,
        connection: Any,
        table_name: str,
        *,
        if_exists: bool = True,
        ch_cluster: str | None = None,
        query_label: str | None = None,
    ) -> None:
        self.execute_command(
            connection,
            self.drop_table_sql(
                table_name,
                if_exists=if_exists,
                ch_cluster=ch_cluster,
                query_label=query_label,
            ),
        )

    def analyze_table_sql(
        self,
        table_name: str,
        *,
        query_label: str | None = None,
    ) -> str:
        return _apply_query_label(f"ANALYZE {table_name}", query_label)

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
            cursor.execute(
                self.count_table_rows_sql(table_name, query_label=query_label)
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0
        finally:
            cursor.close()

    def get_table_column_types(
        self,
        connection: Any,
        table_name: str,
        *,
        connection_key: str,
    ) -> dict[str, str]:
        raise NotImplementedError

    def inspect_source_query_schema(self, connection: Any, query: str) -> list[Any]:
        raise NotImplementedError

    def map_source_type_to_target(self, column: Any) -> str:
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
    ) -> list[str]:
        raise NotImplementedError

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

    def build_stage_duplicate_keys_sql(
        self,
        stage_table: str,
        key_columns: Sequence[str],
    ) -> str:
        key_sql = self.column_list_sql(key_columns)
        return (
            f"SELECT 1 FROM {stage_table} "
            f"GROUP BY {key_sql} "
            "HAVING COUNT(*) > 1 "
            "LIMIT 1"
        )

    def build_stage_target_key_overlap_sql(
        self,
        stage_table: str,
        target_table: str,
        key_columns: Sequence[str],
    ) -> str:
        join_condition = " AND ".join(
            self.null_safe_key_equality("stage_src", "target_dst", column_name)
            for column_name in key_columns
        )
        return (
            "SELECT 1 "
            f"FROM {stage_table} AS stage_src "
            f"INNER JOIN {target_table} AS target_dst ON {join_condition} "
            "LIMIT 1"
        )

    def stage_has_duplicate_keys(
        self,
        connection: Any,
        stage_table: str,
        key_columns: Sequence[str],
    ) -> bool:
        return self.query_has_rows(
            connection,
            self.build_stage_duplicate_keys_sql(stage_table, key_columns),
        )

    def stage_keys_overlap_target(
        self,
        connection: Any,
        stage_table: str,
        target_table: str,
        key_columns: Sequence[str],
    ) -> bool:
        return self.query_has_rows(
            connection,
            self.build_stage_target_key_overlap_sql(
                stage_table,
                target_table,
                key_columns,
            ),
        )

    def query_has_rows(self, connection: Any, sql: str) -> bool:
        cursor = connection.cursor()
        try:
            cursor.execute(sql)
            return cursor.fetchone() is not None
        finally:
            cursor.close()

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
        return (
            f"INSERT INTO {target_table} ({target_columns}) "
            f"SELECT {select_columns} {from_sql}"
        )

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
        return (
            f"({left_expr} = {right_expr} "
            f"OR ({left_expr} IS NULL AND {right_expr} IS NULL))"
        )

    def quote_identifier(self, identifier: str) -> str:
        from ..core.identifiers import quote_identifier_part

        return quote_identifier_part(identifier, self.backend)


def _apply_query_label(sql: str, query_label: str | None) -> str:
    from ..execution.labels import apply_query_label

    return apply_query_label(sql, query_label)
