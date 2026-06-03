from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from analytics_toolkit.general import time_print

from .backend_adapters import split_trino_table_name
from .connection.config import get_connection_config
from .connection.errors import InvalidSqlInputError
from .connection.get_sql_connection import get_sql_connection
from .ddl.create_sql_table import build_ch_shard_table_name
from .dml.table.table_ops import (
    count_table_rows,
    get_table_column_types,
    table_exists,
)
from .operation_runner import timed_public_sql_function


@dataclass(frozen=True)
class SqlTableInfo:
    connection_key: str
    backend: str
    table: str
    exists: bool
    columns: dict[str, str]
    row_count: int | None
    resolved_table: str | None
    shard_table: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "connection_key": self.connection_key,
            "backend": self.backend,
            "table": self.table,
            "exists": self.exists,
            "columns": dict(self.columns),
            "row_count": self.row_count,
            "resolved_table": self.resolved_table,
            "shard_table": self.shard_table,
        }

    def to_frame(self) -> pd.DataFrame:
        base_row = {
            "connection_key": self.connection_key,
            "backend": self.backend,
            "table": self.table,
            "exists": self.exists,
            "row_count": self.row_count,
            "resolved_table": self.resolved_table,
            "shard_table": self.shard_table,
        }
        rows = [
            {
                **base_row,
                "column_name": column_name,
                "column_type": column_type,
            }
            for column_name, column_type in self.columns.items()
        ]
        if not rows:
            rows.append(
                {
                    **base_row,
                    "column_name": None,
                    "column_type": None,
                }
            )
        return pd.DataFrame(
            rows,
            columns=[
                "connection_key",
                "backend",
                "table",
                "exists",
                "row_count",
                "resolved_table",
                "shard_table",
                "column_name",
                "column_type",
            ],
        )


@timed_public_sql_function
def table_info(
    db_key: str,
    table: str,
    include_row_count: bool = False,
) -> SqlTableInfo:
    if not isinstance(include_row_count, bool):
        raise ValueError("include_row_count must be a boolean.")

    config = get_connection_config(db_key)
    table_name = _validate_table_name(table)
    resolved_table = _resolve_table_name(
        config.connection_key,
        config.backend,
        table_name,
    )
    shard_table = (
        build_ch_shard_table_name(table_name)
        if config.backend == "ch"
        else None
    )
    inspection_table = resolved_table or table_name

    connection = get_sql_connection(config.connection_key)
    try:
        exists = table_exists(
            config.backend,
            connection,
            inspection_table,
            connection_key=config.connection_key,
        )
        if not exists:
            return SqlTableInfo(
                connection_key=config.connection_key,
                backend=config.backend,
                table=table_name,
                exists=False,
                columns={},
                row_count=None,
                resolved_table=resolved_table,
                shard_table=shard_table,
            )

        columns = get_table_column_types(
            config.backend,
            connection,
            inspection_table,
            connection_key=config.connection_key,
        )
        row_count = (
            count_table_rows(config.backend, connection, inspection_table)
            if include_row_count
            else None
        )
        return SqlTableInfo(
            connection_key=config.connection_key,
            backend=config.backend,
            table=table_name,
            exists=True,
            columns=columns,
            row_count=row_count,
            resolved_table=resolved_table,
            shard_table=shard_table,
        )
    finally:
        time_print(
            f"Closing {config.connection_key} connection",
            connection=config.connection_key,
            backend=config.backend,
            phase="close",
        )
        connection.close()


def _validate_table_name(table: str) -> str:
    normalized = str(table).strip()
    if not normalized:
        raise InvalidSqlInputError("Table name must not be empty.")
    return normalized


def _resolve_table_name(
    connection_key: str,
    backend: str,
    table_name: str,
) -> str | None:
    if backend != "trino":
        return None
    catalog, schema_name, relation_name = split_trino_table_name(
        table_name,
        connection_key=connection_key,
    )
    return f"{catalog}.{schema_name}.{relation_name}"
