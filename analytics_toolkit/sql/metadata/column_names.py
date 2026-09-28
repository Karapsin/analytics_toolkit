"""Internal SELECT-only table column discovery for editor completion."""

from __future__ import annotations

from ..backends import get_backend_adapter
from ..connection.config import get_connection_config
from ..connection.get_sql_connection import get_sql_connection
from ..execution.cancellation import raise_if_cancelled
from ..execution.metadata_cancellation import cancellable_metadata_connection


def table_column_names(db_key: str, table: str) -> tuple[str, ...]:
    """Read one row's result metadata once, without retries or catalog fallback."""
    raise_if_cancelled()
    config = get_connection_config(db_key)
    adapter = get_backend_adapter(config.backend)
    connection = get_sql_connection(config.connection_key)
    try:
        names = adapter.get_table_column_names(
            cancellable_metadata_connection(connection),
            table,
            connection_key=config.connection_key,
        )
        raise_if_cancelled()
        return names
    finally:
        connection.close()
