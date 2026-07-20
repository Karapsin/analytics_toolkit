from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .ddl import _format_ch_cluster_name

if TYPE_CHECKING:
    from collections.abc import Sequence


SYNCHRONOUS_CLUSTER_COMMAND_SETTINGS = {
    "distributed_ddl_task_timeout": 300,
    "distributed_ddl_output_mode": "throw_only_active",
}


def cluster_clause(ch_cluster: str | None) -> str:
    if ch_cluster is None:
        return ""
    return f" ON CLUSTER {_format_ch_cluster_name(ch_cluster)}"


def execute_reconfiguration_sqls(
    adapter: Any,
    connection: Any,
    sqls: Sequence[str],
) -> None:
    for sql in sqls:
        if "ON CLUSTER" not in sql:
            adapter.execute_command(connection, sql)
            continue
        try:
            connection.command(
                sql,
                settings=SYNCHRONOUS_CLUSTER_COMMAND_SETTINGS,
            )
        except TypeError:
            adapter.execute_command(connection, sql)


__all__ = ["cluster_clause", "execute_reconfiguration_sqls"]
