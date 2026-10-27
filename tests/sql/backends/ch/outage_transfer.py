"""Public transfer orchestration keeps partial source work away from destructive targets."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.transfer.flow import row_counts
from analytics_toolkit.sql.execution.backend_options import backend_operation


@pytest.mark.parametrize("write_mode", ["replace", "upsert", "truncate_insert"])
def test_outage_transfer_blocks_destructive_target_before_staging(
    outage: Any, write_mode: str
) -> None:
    cluster, configure = outage
    configure(aliases=("source", "target"), transfer_staging_schema="db")
    cluster.alive.remove("h2")
    kwargs = (
        {"key_columns": ["id"], "upsert_partition_column": "id"} if write_mode == "upsert" else {}
    )
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterUnavailableError
    ):
        sql.transfer(
            from_db="source",
            to_db="target",
            from_sql="SELECT id FROM db.events",
            to_table="db.events",
            write_mode=write_mode,
            ch_unavailable_shards="healthy_shards",
            table_schema={"id": "UInt64"},
            retry_cnt=1,
            full_retry_cnt=1,
            timeout_increment=0,
            full_timeout_increment=0,
            **kwargs,
        )
    assert not cluster.commands
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,)]


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_outage_uncertain_destination_retains_materialized_source_and_closes_connection(
    monkeypatch: Any, backend: str
) -> None:
    closed = []
    monkeypatch.setattr(row_counts, "close_connection_ref", lambda *args: closed.append(args))
    options = SimpleNamespace(from_db_backend=backend, from_db_key="source")
    stages = SimpleNamespace(source_stage_tables=["db.saved_source"])
    connection_ref = {"connection": object()}
    with backend_operation() as state:
        state.preserve_recovery = True
        row_counts.cleanup_sources_and_close(options, connection_ref, stages)
    assert stages.source_stage_tables == ["db.saved_source"]
    assert closed == [(connection_ref, "source", "source")]
