"""Acknowledgement-bound journals for nontransactional table replacement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError
from analytics_toolkit.sql.execution.backend_options import current_backend_operation


@dataclass(frozen=True)
class ReplacementCutover:
    replacement: str
    backup: str
    expected_rows: int
    statements: tuple[tuple[str, str], ...]


def _retain(journal: dict[str, Any]) -> None:
    journal["recovery_retained"] = True
    state = current_backend_operation()
    if state is not None:
        state.preserve_recovery = True
        state.structural_replay_blocked = True


def _ambiguous(
    journal: dict[str, Any],
    *,
    rollback_error: BaseException | None = None,
) -> AmbiguousSqlReplaceError:
    _retain(journal)
    phase = (
        "Replacement and automatic rollback both failed"
        if rollback_error
        else ("Replacement rename acknowledgement is uncertain")
    )
    message = (
        f"{phase}; structural replay stopped. Recovery resources were preserved: "
        f"target={journal['target']}, replacement={journal['replacement']}, "
        f"backup={journal['backup']}, incoming={journal['incoming']}, "
        f"connection={journal['connection_key']}."
    )
    error = AmbiguousSqlReplaceError(message)
    cast("Any", error).analytics_toolkit_sql_cutover_journal = journal
    if isinstance(rollback_error, Exception):
        error.rollback_error = rollback_error
    return error


def execute_replacement_cutover(adapter: Any, request: Any, cutover: ReplacementCutover) -> None:
    """Never infer that an unacknowledged rename failed to modify the catalog."""
    journal = _new_journal(adapter, request, cutover)
    for step in journal["steps"]:
        _submit_cutover_step(adapter, request, step, journal)
    try:
        _verify_final_count(adapter, request, cutover.expected_rows)
    except BaseException as original:
        _rollback_cutover(adapter, request, journal, original)
        raise


def _new_journal(adapter: Any, request: Any, cutover: ReplacementCutover) -> dict[str, Any]:
    journal: dict[str, Any] = {
        "backend": adapter.backend,
        "connection_key": request.connection_key,
        "target": request.target_table,
        "replacement": cutover.replacement,
        "backup": cutover.backup,
        "incoming": request.stage_table,
        "recovery_retained": False,
        "steps": [
            {
                "sql": sql,
                "inverse": inverse,
                "submitted": False,
                "acknowledged": False,
                "rollback_submitted": False,
                "rollback_acknowledged": False,
            }
            for sql, inverse in cutover.statements
        ],
    }
    state = current_backend_operation()
    if state is not None:
        with state.lock:
            state.resources[
                (
                    "replacement_cutover",
                    adapter.backend,
                    request.connection_key,
                    request.target_table,
                    cutover.replacement,
                )
            ] = journal
    return journal


def _submit_cutover_step(
    adapter: Any, request: Any, step: dict[str, Any], journal: dict[str, Any]
) -> None:
    step["submitted"] = True
    try:
        adapter.execute_command(request.connection, step["sql"])
    except BaseException as original:
        _retain(journal)
        if isinstance(original, Exception):
            raise _ambiguous(journal) from original
        raise
    step["acknowledged"] = True


def _verify_final_count(adapter: Any, request: Any, expected_rows: int) -> None:
    if adapter.count_table_rows(request.connection, request.target_table) != expected_rows:
        message = "Final replacement row count does not match the staged row count."
        raise RuntimeError(message)


def _rollback_cutover(
    adapter: Any, request: Any, journal: dict[str, Any], original: BaseException
) -> None:
    for step in reversed(journal["steps"]):
        if not step["acknowledged"] or step["rollback_acknowledged"]:
            continue
        step["rollback_submitted"] = True
        try:
            adapter.execute_command(request.connection, step["inverse"])
        # Keep the primary failure, including an interruption, after uncertain rollback.
        except BaseException as rollback_error:  # noqa: BLE001 - preserve primary failure.
            _retain(journal)
            if isinstance(original, Exception):
                raise _ambiguous(journal, rollback_error=rollback_error) from original
            raise original.with_traceback(original.__traceback__) from None
        step["rollback_acknowledged"] = True
