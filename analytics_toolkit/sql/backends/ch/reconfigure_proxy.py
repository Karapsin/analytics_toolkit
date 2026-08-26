from __future__ import annotations

from typing import Any

from .routing import local_sql


def plan(adapter: Any, connection: Any, options: Any) -> Any:
    from .reconfigure import plan_ch_table_reconfiguration  # noqa: PLC0415

    with local_sql(connection):
        return plan_ch_table_reconfiguration(adapter, connection, options)


def execute(
    adapter: Any,
    connection: Any,
    reconfiguration: Any,
    *,
    validate_row_count: bool,
) -> None:
    from .reconfigure import execute_ch_table_reconfiguration  # noqa: PLC0415

    with local_sql(connection):
        execute_ch_table_reconfiguration(
            adapter,
            connection,
            reconfiguration,
            validate_row_count=validate_row_count,
        )
