"""The original keyed shared/worker creation paths register acknowledgements."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.connection.errors import SqlTableReadinessError
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    TransferConnectionRefs,
    TransferStageState,
)

from tests.sql._support.row_batches import (
    attempt_module,
    finalize_module,
    keys_module,
    make_progress_options,
)


@pytest.mark.parametrize("parallel_workers", [False, True])
def test_keyed_initialization_registers_partial_pair_before_readiness_returns(
    monkeypatch: pytest.MonkeyPatch, parallel_workers: bool
) -> None:
    slices = [
        keys_module.build_transfer_slice(
            index=index,
            source_sql="SELECT id FROM source WHERE {id}",
            transfer_keys=[keys_module.TransferKey("id", "id")],
            values=(index,),
        )
        for index in range(2)
    ]
    options = make_progress_options(
        to_db_key="ch",
        to_db_backend="ch",
        target_table="db.target",
        write_mode="append",
        table_schema={"id": "Int64"},
        transfer_slices=slices,
    )
    options = replace(
        options,
        transfer_concurrency=replace(
            options.transfer_concurrency,
            effective_write=2 if parallel_workers else 1,
        ),
    )
    state = TransferStageState(target_exists=True)
    commands: list[str] = []
    connection = SimpleNamespace(command=commands.append)

    def create_stage(**kwargs: Any) -> str:
        kwargs["on_stage_candidate"]("db.partial_stage")
        kwargs["on_resource_created"]("db.partial_stage_shard")
        message = "shard created before readiness failed"
        raise SqlTableReadinessError(message)

    monkeypatch.setattr(attempt_module, "create_stage_table", create_stage)
    with pytest.raises(SqlTableReadinessError):
        attempt_module.initialize_shared_stage_for_keyed_slices(
            options=options,
            connection_refs=TransferConnectionRefs(target={"connection": connection}),
            stage_state=state,
            source_schema=[SimpleNamespace(name="id")],
        )
    assert state.stage_table is None
    assert not state.stage_table_created
    assert state.stage_table_candidates == ["db.partial_stage"]
    assert state.owned_stage_resources == [("db.partial_stage_shard", "db.partial_stage")]
    monkeypatch.setattr(
        finalize_module,
        "_run_target_operation",
        lambda _options, _role, operation, **_kwargs: operation({"connection": connection}),
    )
    finalize_module.cleanup_stage(options, object(), state, 1)
    assert commands == ["DROP TABLE IF EXISTS `db`.`partial_stage_shard`"]
