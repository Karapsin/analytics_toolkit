from __future__ import annotations

# ruff: noqa: PLR0913, TID252
from typing import TYPE_CHECKING, Any

from analytics_toolkit.general import time_print

from .logging import pipeline_phase_message

if TYPE_CHECKING:
    from ..runtime.models import TransferConnectionRefs, TransferOptions, TransferStageState


def finish_keyed_transfer(
    *,
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    worker_stage_states: list[Any],
    stage_state: TransferStageState,
    total_rows: int,
    open_connection: Any,
    consolidate: Any,
    validate: Any,
    finalize: Any,
) -> None:
    _run_phase(
        "worker-stage consolidation",
        lambda: consolidate(
            options=options,
            connection_refs=connection_refs,
            worker_stage_states=worker_stage_states,
            stage_state=stage_state,
        ),
    )
    _run_phase(
        "source/stage row-count validation",
        lambda: validate(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
            total_rows=total_rows,
            open_connection=open_connection,
        ),
    )
    _run_phase(
        "target finalization",
        lambda: finalize(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
            total_rows=total_rows,
        ),
    )


def _run_phase(name: str, operation: Any) -> None:
    time_print(pipeline_phase_message(name))
    operation()
    time_print(pipeline_phase_message(name, complete=True))
