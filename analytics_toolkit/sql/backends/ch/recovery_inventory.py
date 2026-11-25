"""Keep structural recovery names and submission progress on public failures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.execution.backend_options import current_backend_operation

from .reconfigure_support import drop_table_sql

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping


def structural_replay_blocked() -> bool:
    state = current_backend_operation()
    return state is not None and state.structural_replay_blocked


def retain_structural_recovery() -> None:
    state = current_backend_operation()
    if state is not None:
        state.preserve_recovery = True
        state.structural_replay_blocked = True


def attach_recovery_details(
    error: BaseException,
    *,
    scopes: Iterable[tuple[str, str | None]],
    cutover_journal: Iterable[Mapping[str, Any]] = (),
    create_journal: Iterable[Mapping[str, Any]] = (),
    cleanup_errors: Iterable[BaseException] = (),
) -> None:
    """Copy diagnostic state; never turn a candidate or submission into ownership."""
    previous = getattr(error, "analytics_toolkit_sql_recovery_table_scopes", ())
    error.__dict__["analytics_toolkit_sql_recovery_table_scopes"] = tuple(
        dict.fromkeys([*previous, *scopes])
    )
    error.__dict__["analytics_toolkit_sql_cutover_journal"] = tuple(
        dict(step) for step in cutover_journal
    )
    error.__dict__["analytics_toolkit_sql_create_submission_journal"] = tuple(
        dict(step) for step in create_journal
    )
    previous_errors = getattr(error, "analytics_toolkit_sql_cleanup_errors", ())
    error.__dict__["analytics_toolkit_sql_cleanup_errors"] = (
        *previous_errors,
        *cleanup_errors,
    )


def attach_reconfiguration_recovery(
    error: BaseException, reconfiguration: Any, *, cleanup_errors: Iterable[BaseException] = ()
) -> None:
    creates = getattr(reconfiguration, "create_submission_journal", ())
    cutovers = reconfiguration.cutover_journal
    scopes = [(step["table"], step["cluster"]) for step in creates if step.get("submitted")]
    scopes.extend(reconfiguration.owned_table_scopes)
    if cutovers:
        scopes.extend(reconfiguration.temporary_table_scopes)
        scopes.extend(getattr(reconfiguration, "backup_table_scopes", ()))
    attach_recovery_details(
        error,
        scopes=scopes,
        cutover_journal=cutovers,
        create_journal=creates,
        cleanup_errors=cleanup_errors,
    )


def recover_reconfiguration_failure(
    error: BaseException,
    reconfiguration: Any,
    *,
    uncertain: bool,
    rollback: Callable[[], None] | None,
    cleanup: Callable[[], Iterable[BaseException]],
) -> None:
    if uncertain or structural_replay_blocked() or not isinstance(error, Exception):
        attach_reconfiguration_recovery(error, reconfiguration)
        return
    if rollback is not None:
        try:
            rollback()
        except BaseException as rollback_error:
            attach_reconfiguration_recovery(rollback_error, reconfiguration)
            raise rollback_error from error
    try:
        cleanup_errors = tuple(cleanup())
    except BaseException as cleanup_error:
        retain_structural_recovery()
        attach_reconfiguration_recovery(cleanup_error, reconfiguration)
        raise cleanup_error from error
    if cleanup_errors:
        retain_structural_recovery()
        attach_reconfiguration_recovery(error, reconfiguration, cleanup_errors=cleanup_errors)


def cleanup_created_resources(
    adapter: Any, connection: Any, reconfiguration: Any
) -> list[Exception]:
    errors = []
    for resource in reconfiguration.owned_table_scopes:
        error = _cleanup_resource(adapter, connection, resource)
        if error is not None:
            errors.append(error)
            if structural_replay_blocked():
                break
    return errors


def _cleanup_resource(
    adapter: Any, connection: Any, resource: tuple[str, str | None]
) -> Exception | None:
    table, cluster = resource
    try:
        adapter.execute_command(connection, drop_table_sql(table, cluster))
    except BaseException as error:
        if isinstance(error, Exception):
            return error
        raise
    return None


__all__ = [
    "attach_reconfiguration_recovery",
    "attach_recovery_details",
    "cleanup_created_resources",
    "recover_reconfiguration_failure",
    "retain_structural_recovery",
    "structural_replay_blocked",
]
