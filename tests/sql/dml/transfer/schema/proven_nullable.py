"""Metadata-proven nullable domains remain authoritative in the real stage initializer."""

from __future__ import annotations

import struct
from dataclasses import replace

import pytest
from analytics_toolkit.sql.dml.transfer.flow import stage
from analytics_toolkit.sql.dml.transfer.io.source import normalize_source_batch

from tests.sql._support.adaptive_transfer import (
    adaptive_input,
    attempt,
    require_streaming_fast_path,
)
from tests.sql._support.ch_transfer_client_validation import (
    ClientConnection,
    insert_client_rows,
    install_client_transfer,
)


@pytest.mark.parametrize("transport", ["native", "http"])
def test_public_declared_fit_transfer_keeps_nullable_domain_before_later_null(
    monkeypatch, write_sql_connections, transport
):
    schema = {"id": "Nullable(Int64)"}
    scenario, target = install_client_transfer(
        monkeypatch,
        write_sql_connections,
        source_types=schema,
        target_types=schema,
        rows=[(1,), (None,)],
        transport=transport,
    )
    scenario.options = replace(scenario.options, table_schema=None)
    require_streaming_fast_path(monkeypatch)
    created = []

    def create_stage(**kwargs):
        declared = dict(kwargs["column_types"])
        created.append(declared)
        target.schema = declared
        target.physical_schema = declared
        scenario.events.append("create_stage")
        return "sandbox.private_payload"

    def load(options, connection_refs, stage_state, *_args, **_kwargs):
        refs, state = connection_refs, stage_state
        refs.target["connection"] = ClientConnection(target)
        rows = []
        for raw in adaptive_input.resolved_source_batches(
            options,
            scenario.read,
            options.from_db_key,
            options.from_db_backend,
            refs.source,
            options.source_sql,
            options.batch_size,
        ):
            batch = normalize_source_batch(options, refs.source["connection"], raw, schema)
            if state.stage_table is None:
                stage.initialize_stage_for_first_batch(options, refs, state, batch)
            insert_client_rows(target, state.stage_column_types, batch.rows)
            rows.extend(batch.rows)
        scenario.loaded.append(rows)
        return len(rows)

    monkeypatch.setattr(stage, "create_stage_table", create_stage)
    monkeypatch.setattr(attempt, "load_stage_batches", load)

    result = scenario.run(metadata=True)

    assert result.rows == 2
    assert created == [schema]
    assert target.emitted_batches == [[(1,)], [(None,)]]
    assert target.wire_columns == [
        ("id", "Nullable(Int64)", b"\x00" + struct.pack("<q", 1)),
        ("id", "Nullable(Int64)", b"\x01" + bytes(8)),
    ]
    assert target.live_rows == [(1,), (None,)]
    assert scenario.read_count == scenario.source_open_count == 1
    assert scenario.events.index("create_stage") < scenario.events.index(
        f"complete:{scenario.options.source_sql}"
    )
    assert "spool" not in scenario.options.input_resolution
