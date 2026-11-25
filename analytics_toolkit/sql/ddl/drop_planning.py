"""Keep unresolved existing-resource ownership out of executable creation plans."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

if TYPE_CHECKING:
    from analytics_toolkit.sql.execution.plans import SqlPlan


def pending_creation_drop_options(adapter: Any, *, requested: bool) -> dict[str, Any]:
    if not requested:
        return {}
    hook = getattr(adapter, "pending_drop_plan_options", None)
    return dict(hook()) if callable(hook) else {}


def require_resolved_creation_drop_sql(adapter: Any, *, requested: bool) -> None:
    """A string cannot carry the required execution-time ownership resolution step."""
    if pending_creation_drop_options(adapter, requested=requested):
        message = (
            "SQL-only generation cannot certify existing ClickHouse drop ownership and scope. "
            "Execute the operation for runtime resource verification, or use dry_run/return_sql "
            "to inspect a plan with pending ownership resolution."
        )
        raise InvalidSqlInputError(message)


def apply_pending_creation_drop(plan: SqlPlan, adapter: Any, *, requested: bool) -> None:
    pending = pending_creation_drop_options(adapter, requested=requested)
    if pending:
        plan.options.update(pending)
        plan.statements[:] = [step for step in plan.statements if step.phase != "drop_target"]
        plan.metadata.statement_count = None


__all__ = [
    "apply_pending_creation_drop",
    "pending_creation_drop_options",
    "require_resolved_creation_drop_sql",
]
