"""Public transfer orchestration keeps partial source work away from destructive targets."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.transfer.flow import row_counts
from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests.sql._support.outage import Client
from tests.sql._support.outage_gp import MemoryGreenplum


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


@pytest.mark.parametrize("direction", ["from_ch", "to_ch"])
def test_outage_public_append_transfer_validates_observed_rows_in_both_directions(
    outage: Any, monkeypatch: Any, direction: str
) -> None:
    cluster, configure = outage
    configure(
        aliases=("ch", "gp"),
        ch_unavailable_shards="healthy_shards",
        transfer_staging_schema="db",
        per_alias={
            "gp": {
                "type": "gp",
                "ddl_defaults": None,
                "cluster_routing": None,
            }
        },
    )
    cluster.alive.remove("h2")
    greenplum = MemoryGreenplum()
    greenplum.install(monkeypatch)
    from_db, to_db = ("ch", "gp") if direction == "from_ch" else ("gp", "ch")
    source = "SELECT id FROM db.events" if direction == "from_ch" else "SELECT id FROM db.source"
    target = "db.target" if direction == "from_ch" else "db.events"
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        result = sql.transfer(
            from_db=from_db,
            to_db=to_db,
            from_sql=source,
            to_table=target,
            write_mode="append",
            ignore_source_staging=True,
            return_metadata=True,
            table_schema={"id": "bigint" if direction == "from_ch" else "UInt64"},
            retry_cnt=1,
            full_retry_cnt=1,
            timeout_increment=0,
            full_timeout_increment=0,
        )
    expected = 1 if direction == "from_ch" else 2
    assert result.inserted_rows == expected
    assert result.metadata.expected_source_rows == expected
    assert result.metadata.row_count_validated is True
    assert result.metadata.data_complete is False
    assert result.metadata.source_data_complete is (direction != "from_ch")
    assert result.metadata.target_data_complete is (direction != "to_ch")
    if direction == "from_ch":
        assert greenplum.tables["db.target"].id.tolist() == [99, 1]
    else:
        assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (10,), (11,)]


def test_outage_transfer_keeps_independent_alias_defaults_and_completeness(outage: Any) -> None:
    cluster, configure = outage
    configure(
        aliases=("source", "target"),
        transfer_staging_schema="db",
        per_alias={
            "source": {
                "cluster_routing": {"cluster": "single"},
                "ddl_defaults": None,
                "ch_unavailable_shards": "stop",
                "transfer_staging_schema": None,
            },
            "target": {"ch_unavailable_shards": "healthy_shards"},
        },
    )
    cluster.members_by_cluster["single"] = [(1, 1, "h1", 9000)]
    cluster.create("h1", "CREATE TABLE db.single_shard (id UInt64) ENGINE=MergeTree ORDER BY id")
    cluster.create(
        "h1",
        "CREATE TABLE db.single (id UInt64) "
        "ENGINE=Distributed('single', 'db', 'single_shard', rand())",
    )
    cluster.tables[("h1", "db.single_shard")].rows = [(10,)]
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        result = sql.transfer(
            from_db="source",
            to_db="target",
            from_sql="SELECT id FROM db.single",
            to_table="db.events",
            write_mode="append",
            ignore_source_staging=True,
            return_metadata=True,
            table_schema={"id": "UInt64"},
            retry_cnt=1,
            full_retry_cnt=1,
            timeout_increment=0,
            full_timeout_increment=0,
        )
    assert result.inserted_rows == 1
    assert result.metadata.source_data_complete is True
    assert result.metadata.target_data_complete is False
    assert {item["alias"] for item in result.metadata.ch_cluster_status} == {"source", "target"}
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (10,)]
    before = list(cluster.commands)
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.transfer(
            from_db="source",
            to_db="target",
            from_sql="SELECT id FROM db.single",
            to_table="db.events",
            write_mode="append",
            ignore_source_staging=True,
            ch_unavailable_shards="stop",
            table_schema={"id": "UInt64"},
            retry_cnt=1,
            full_retry_cnt=1,
            timeout_increment=0,
            full_timeout_increment=0,
        )
    assert cluster.commands == before


def test_outage_observed_source_count_mismatch_prevents_final_target_append(
    outage: Any, monkeypatch: Any
) -> None:
    cluster, configure = outage
    configure(
        aliases=("ch", "gp"),
        ch_unavailable_shards="healthy_shards",
        transfer_staging_schema="db",
        per_alias={
            "gp": {
                "type": "gp",
                "ddl_defaults": None,
                "cluster_routing": None,
            }
        },
    )
    cluster.alive.remove("h2")
    greenplum = MemoryGreenplum()
    greenplum.install(monkeypatch)
    original = Client.query
    changed = False

    def query(client: Any, sql_text: str, **kwargs: Any) -> Any:
        nonlocal changed
        result = original(client, sql_text, **kwargs)
        if "source_count_probe" in sql_text and not changed:
            cluster.tables[("h1", "db.events_shard")].rows.clear()
            changed = True
        return result

    monkeypatch.setattr(Client, "query", query)
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        ValueError, match=r"count|rows"
    ):
        sql.transfer(
            from_db="ch",
            to_db="gp",
            from_sql="SELECT id FROM db.events",
            to_table="db.target",
            write_mode="append",
            ignore_source_staging=True,
            return_metadata=True,
            table_schema={"id": "bigint"},
            retry_cnt=1,
            full_retry_cnt=1,
            timeout_increment=0,
            full_timeout_increment=0,
        )
    assert changed
    assert greenplum.tables["db.target"].id.tolist() == [99]
