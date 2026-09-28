"""Internal SELECT-only table column discovery for editor completion."""

from __future__ import annotations

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection
from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled
from analytics_toolkit.sql.execution.metadata_cancellation import cancellable_metadata_connection


def table_column_names(db_key: str, table: str) -> tuple[str, ...]:
    """Read one row's result metadata once, without retries or catalog fallback."""
    raise_if_cancelled()
    config = get_connection_config(db_key)
    adapter = get_backend_adapter(config.backend)
    connection = get_sql_connection(config.connection_key)
    try:
        names: tuple[str, ...] = adapter.get_table_column_names(
            cancellable_metadata_connection(connection),
            table,
            connection_key=config.connection_key,
        )
        raise_if_cancelled()
        return names
    finally:
        connection.close()
