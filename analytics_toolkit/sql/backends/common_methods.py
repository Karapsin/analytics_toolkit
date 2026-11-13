from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql._log_context import (
    current_sql_log_context,
    prefix_sql_log_message,
)
from analytics_toolkit.sql.connection.validation import close_validation_resource
from analytics_toolkit.sql.execution.labels import apply_query_label

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import pandas as pd


class DataframePreparationPolicy:
    """Default policies leave dataframe payloads and execution previews alone."""

    load_execution_preview_requires_schema: bool = False
    accepts_unparameterized_decimal_floats: bool = False

    def canonical_native_type(self, native_type: str) -> str:
        """Keep backend type identities unless its adapter recognizes an alias."""
        from .decimal_values import decimal_target_type  # noqa: PLC0415 -- native defaults.

        return decimal_target_type(native_type, getattr(self, "backend", ""))

    def prepare_dataframe_payload(
        self, df: pd.DataFrame, schema: Mapping[str, str] | None
    ) -> pd.DataFrame | None:
        """Backends without an owned payload codec need no prepared frame."""
        del self, df, schema
        return None

    def validate_dataframe_staging_schema(
        self, schema: Mapping[str, str], *, use_parquet: bool
    ) -> None:
        """Backends without an alternate dataframe stage have no extra restriction."""
        del self, schema, use_parquet


def column_list_sql(self: Any, columns: Sequence[str]) -> str:
    return ", ".join(self.quote_identifier(column_name) for column_name in columns)


def quote_identifier(self: Any, identifier: str) -> str:
    escaped = identifier.replace(self.identifier_quote, self.identifier_quote * 2)
    return f"{self.identifier_quote}{escaped}{self.identifier_quote}"


def validate_existing_dataframe_insert_columns(  # noqa: PLR0913 -- shared adapter hook signature.
    self: Any,
    connection: Any,
    table_name: str,
    columns: Sequence[str],
    *,
    connection_key: str,
    null_columns: Sequence[str] = (),
    row_count: int | None = None,
) -> None:
    """Backends without computed columns need no additional insertability probe."""
    del self, connection, table_name, columns, connection_key, null_columns, row_count


def get_required_column_defaults(
    self: Any, connection: Any, table_name: str, *, connection_key: str
) -> dict[str, bool]:
    """Return only required-column constraints reported by the backend."""
    del self, connection, table_name, connection_key
    return {}


def cast_select_expression(self: Any, column_name: str, target_type: str) -> str:
    quoted_column = self.quote_identifier(column_name)
    return f"CAST({quoted_column} AS {target_type}) AS {quoted_column}"


def get_table_column_names(
    self: Any, connection: Any, table_name: str, *, connection_key: str
) -> tuple[str, ...]:
    """Read DBAPI column names from one bounded SELECT, including empty results."""
    # Identifier parsing obtains adapters from this module's initializing backend registry.
    from analytics_toolkit.sql.core.identifiers import TableIdentifier  # noqa: PLC0415

    resolved = self.resolve_table_info_table_name(table_name, connection_key=connection_key)
    table = TableIdentifier.parse(resolved or table_name, self.backend).render_quoted(self.backend)
    cursor = connection.cursor()
    try:
        # The table reference was structurally parsed and backend-quoted above.
        cursor.execute(f"SELECT * FROM {table} LIMIT 1")  # noqa: S608
        columns = cursor.description
        if columns is None:
            message = "Column probe returned no result metadata."
            raise ValueError(message)
        names = tuple(str(column[0]) for column in columns)
        cursor.fetchall()  # Consume at most one row; never retain or display its values.
        return names
    finally:
        cursor.close()


def get_writable_table_column_types(
    self: Any, connection: Any, table_name: str, *, connection_key: str
) -> dict[str, str]:
    """Backends without generated columns accept every declared target column."""
    return cast(
        "dict[str, str]",
        self.get_table_column_types(connection, table_name, connection_key=connection_key),
    )


def resolve_upsert_execution_options(
    self: Any, connection: Any, table_name: str, **options: Any
) -> dict[str, Any]:
    del self, connection, table_name
    return options


def resolve_upsert_execution_sqls(
    self: Any, connection: Any, table_name: str, sqls: list[str]
) -> list[str]:
    del self, connection, table_name
    return sqls


def execute_upsert_command(self: Any, connection: Any, table_name: str, sql: str) -> Any:
    del table_name
    return self.execute_command(connection, sql)


def execute_commands(self: Any, connection: Any, sqls: list[str]) -> None:
    for sql in sqls:
        self.execute_command(connection, sql)


def read_dataframe(
    self: Any,
    connection: Any,
    query: str,
    *,
    print_queries: bool,
    print_query: Any,
    read_dbapi_query: Any,
) -> Any:
    _time_print("Reading DataFrame", backend=self.backend)
    try:
        print_query(query, print_queries)
        return self._read_dataframe_impl(connection, query, read_dbapi_query)
    except Exception:
        _time_print(_sql_failure_message(query), backend=self.backend)
        raise


def read_dataframe_impl(
    self: Any,
    connection: Any,
    query: str,
    read_dbapi_query: Any,
) -> Any:
    return read_dbapi_query(connection, query)


def read_columns(  # noqa: PLR0913 - backend API mirrors read_dataframe
    self: Any,
    connection: Any,
    query: str,
    *,
    print_queries: bool,
    print_query: Any,
    read_dbapi_columns: Any,
) -> Any:
    _time_print("Reading column data", backend=self.backend)
    try:
        print_query(query, print_queries)
        return self._read_columns_impl(connection, query, read_dbapi_columns)
    except Exception:
        _time_print(_sql_failure_message(query), backend=self.backend)
        raise


def read_columns_impl(
    self: Any,
    connection: Any,
    query: str,
    read_dbapi_columns: Any,
) -> Any:
    del self
    return read_dbapi_columns(connection, query)


def _time_print(message: str, *, backend: str) -> None:
    from analytics_toolkit.general import time_print

    time_print(prefix_sql_log_message(message), backend=backend)


def _sql_failure_message(query: str) -> str:
    _prefix, suppress_sql = current_sql_log_context()
    if suppress_sql:
        return "Failed SQL (details suppressed)"
    return f"Failed SQL:\n{query}"


def validate_connection(self: Any, connection: Any) -> None:
    """Consume a backend-owned SELECT so lazy clients actually connect."""
    del self
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT 1")
        rows = cursor.fetchall()
        if not rows or rows[0][0] != 1:
            message = "Connectivity probe returned an unexpected result."
            raise RuntimeError(message)
    finally:
        close_validation_resource(cursor)


def build_insert_from_stage_sql(  # noqa: PLR0913 - stable adapter keyword contract
    self: Any,
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    column_types: dict[str, str] | None,
    query_label: str | None,
) -> str:
    typed_columns = self.column_types_for_columns(column_types, columns)

    return apply_query_label(
        self.build_explicit_insert_from_stage_sql(
            target_table,
            stage_table,
            columns=columns,
            column_types=typed_columns,
        ),
        query_label,
    )


def local_creation_context(self: Any, connection: Any, *, local: bool) -> Any:
    del self, connection, local
    return nullcontext()


def preflight_create_table_replacement(self: Any, connection: Any, options: Any) -> None:
    del self, connection, options


def build_create_table_placeholder_sql(
    self: Any,
    table_name: str,
    *,
    replace_table: bool = False,
) -> str:
    del self
    if replace_table:
        message = "This backend does not support replace-style CREATE TABLE generation."
        raise ValueError(message)
    # This incomplete diagnostic statement is only rendered into a pending SQL plan.
    return f"CREATE TABLE {table_name} (<source query schema>)"
