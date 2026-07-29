from __future__ import annotations

from ..runtime.models import TransferOptions, TransferSlice, TransferStageState


class WorkerStageState:
    def __init__(
        self,
        *,
        worker_index: int,
        stage_state: TransferStageState,
        transfer_slices: list[TransferSlice],
    ) -> None:
        self.worker_index = worker_index
        self.stage_state = stage_state
        self.transfer_slices = transfer_slices


def build_keyed_worker_stage_states(
    *,
    options: TransferOptions | None = None,
    stage_state: TransferStageState,
) -> list[WorkerStageState]:
    transfer_slices = (
        options.transfer_slices
        if options is not None
        else getattr(stage_state, "transfer_slices", None)
    ) or []
    stage_tables = stage_state.stage_tables or (
        [stage_state.stage_table] if stage_state.stage_table is not None else []
    )
    worker_count = len(stage_tables)
    if worker_count == 0:
        raise RuntimeError("Expected stage table to be initialized.")
    return [
        WorkerStageState(
            worker_index=worker_index,
            stage_state=_copy_stage_state_for_worker(
                stage_state,
                stage_table=stage_tables[worker_index],
            ),
            transfer_slices=transfer_slices[worker_index::worker_count],
        )
        for worker_index in range(worker_count)
    ]


def _copy_stage_state_for_worker(
    stage_state: TransferStageState,
    *,
    stage_table: str,
) -> TransferStageState:
    return TransferStageState(
        target_exists=stage_state.target_exists,
        stage_table_created=stage_state.stage_table_created,
        first_non_empty_batch=stage_state.first_non_empty_batch,
        source_column_types=stage_state.source_column_types,
        stage_column_types=stage_state.stage_column_types,
        insert_column_types=stage_state.insert_column_types,
        stage_table=stage_table,
        stage_tables=[stage_table],
        stage_external_location=stage_state.stage_external_location,
        source_columns=list(stage_state.source_columns),
        internal_columns=stage_state.internal_columns,
        transfer_id=stage_state.transfer_id,
        canonical_destination_identity=stage_state.canonical_destination_identity,
    )
