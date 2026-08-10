from __future__ import annotations

from typing import Any, Callable, cast

from .wait import _wait_for_ch_table, _wait_for_ch_table_on_cluster
from .wait_policy import waits_for_distributed, waits_for_shard


def wait_for_created_replacements(
    connection: Any,
    reconfiguration: Any,
    *,
    wait_local: Callable[..., None] = _wait_for_ch_table,
    wait_cluster: Callable[..., None] = _wait_for_ch_table_on_cluster,
) -> None:
    roles = _tag_replacement_roles(reconfiguration)
    plan = getattr(reconfiguration, "plan", None)
    policy = str(getattr(plan, "options", {}).get("ch_ddl_wait_policy", "wait_all"))
    for table_name, cluster, role in roles:
        if role == "shard" and not waits_for_shard(policy):
            continue
        if role == "distributed" and not waits_for_distributed(policy):
            continue
        if cluster is None:
            wait_local(connection, table_name)
        else:
            wait_cluster(connection, table_name, ch_cluster=cluster)


def _tag_replacement_roles(reconfiguration: Any) -> list[tuple[str, str | None, str]]:
    if getattr(reconfiguration, "temporary_table_roles", None):
        return cast(
            "list[tuple[str, str | None, str]]",
            reconfiguration.temporary_table_roles,
        )
    plan = getattr(reconfiguration, "plan", None)
    create_sqls = [
        statement.sql
        for statement in getattr(plan, "statements", [])
        if statement.phase == "create_replacement"
    ]
    scopes = getattr(reconfiguration, "temporary_table_scopes", None)
    if not scopes:
        replacement = getattr(reconfiguration, "replacement_table", None)
        if replacement is None:
            return []
        if (
            getattr(reconfiguration, "strategy", None) == "cross_cluster_rebuild"
            and getattr(reconfiguration, "target_cluster", None) is not None
        ):
            scopes = [
                (table_name, reconfiguration.target_cluster)
                for table_name in getattr(reconfiguration, "temporary_tables", [])
            ]
        else:
            scopes = [(replacement, None)]
    roles = [
        (table_name, cluster, _created_table_role(table_name, create_sqls))
        for table_name, cluster in scopes
    ]
    if hasattr(reconfiguration, "temporary_table_roles"):
        reconfiguration.temporary_table_roles = roles
    return roles


def _created_table_role(table_name: str, create_sqls: list[str]) -> str:
    matching = next((sql for sql in create_sqls if table_name in sql), "")
    return "distributed" if "DISTRIBUTED(" in matching.upper() else "shard"


__all__ = ["wait_for_created_replacements"]
