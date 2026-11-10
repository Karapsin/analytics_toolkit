"""Acknowledged structural cutovers and conservative recovery on lost acknowledgements."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.execution.backend_options import current_backend_operation
from analytics_toolkit.sql.execution.labels import apply_query_label

from .reconfigure_ddl import parse_create_table
from .reconfigure_execution import execute_reconfiguration_sqls
from .reconfigure_support import unquoted_table_name


def create_replacements(adapter: Any, connection: Any, reconfiguration: Any) -> None:
    sqls = [
        statement.sql
        for statement in reconfiguration.plan.statements
        if statement.phase == "create_replacement"
    ]
    for sql in sqls:
        create = parse_create_table(sql, "replacement")
        name = create.this.this.sql(dialect="clickhouse")
        execute_reconfiguration_sqls(adapter, connection, [sql])
        if not create.args.get("exists"):
            for table, scope in reconfiguration.temporary_table_scopes:
                if unquoted_table_name(table) == unquoted_table_name(name):
                    reconfiguration.owned_table_scopes.append((table, scope))
                    break


class StructuralCutoverError(RuntimeError):
    analytics_toolkit_sql_retry_safe = False


def execute_cutover(adapter: Any, connection: Any, reconfiguration: Any) -> None:
    inverses = list(reversed(reconfiguration.rollback_sqls))
    if len(inverses) != len(reconfiguration.cutover_sqls):
        message = "Cutover has no complete inverse journal; no DDL submitted."
        raise StructuralCutoverError(message)
    for sql, inverse in zip(reconfiguration.cutover_sqls, inverses):
        step = {"sql": sql, "inverse": inverse, "submitted": True, "acknowledged": False}
        reconfiguration.cutover_journal.append(step)
        try:
            execute_reconfiguration_sqls(
                adapter,
                connection,
                [apply_query_label(sql, reconfiguration.plan.metadata.query_label)],
            )
        except Exception as exc:
            retain_recovery(reconfiguration)
            message = (
                "ClickHouse cutover acknowledgement is uncertain; structural replay stopped. "
                f"Retained recovery resources: {reconfiguration.temporary_table_scopes!r}; "
                f"backups: {reconfiguration.backup_tables!r}. Original failure: {exc}"
            )
            raise StructuralCutoverError(message) from exc
        step["acknowledged"] = True


def rollback_cutover(adapter: Any, connection: Any, reconfiguration: Any) -> None:
    for step in reversed(reconfiguration.cutover_journal):
        if not step["acknowledged"] or step.get("rollback_acknowledged"):
            continue
        step["rollback_submitted"] = True
        try:
            execute_reconfiguration_sqls(
                adapter,
                connection,
                [apply_query_label(step["inverse"], reconfiguration.plan.metadata.query_label)],
            )
        except Exception as exc:
            retain_recovery(reconfiguration)
            message = (
                "ClickHouse automatic rollback also failed; retained recovery resources: "
                f"{reconfiguration.temporary_table_scopes!r}; "
                f"backups: {reconfiguration.backup_tables!r}. Rollback failure: {exc}"
            )
            raise StructuralCutoverError(message) from exc
        step["rollback_acknowledged"] = True


def retain_recovery(reconfiguration: Any) -> None:
    state = current_backend_operation()
    if state is not None:
        state.preserve_recovery = True
        state.structural_replay_blocked = True
    reconfiguration.cleanup_error = (
        "Recovery artifacts retained after uncertain structural cutover."
    )


__all__ = ["StructuralCutoverError", "create_replacements", "execute_cutover", "rollback_cutover"]
