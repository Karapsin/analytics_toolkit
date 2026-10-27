"""Outage policies cannot be weakened by transport settings or uncertain retries."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.transfer.runtime.retry import is_non_retryable_sql_error
from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests._support.paths import REPO_ROOT


def test_outage_replica_failover_keeps_logical_coverage(outage: Any) -> None:
    cluster, _ = outage
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    cluster.seed(replicated=True)
    cluster.alive.remove("h2")
    result = sql.read("ch", "SELECT id FROM db.events", return_metadata=True)
    assert result.data.id.tolist() == [1]
    assert result.metadata.data_complete is True
    assert result.metadata.ch_cluster_status[-1]["host_coverage_complete"] is False


def test_outage_readonly_replica_is_readable_but_not_writable(outage: Any) -> None:
    cluster, _ = outage
    cluster.tables.clear()
    cluster.seed(replicated=True)
    cluster.replica_readonly.add("h1")
    assert sql.read("ch", "SELECT id FROM db.events").id.tolist() == [1, 2]
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.execute("ch", "INSERT INTO db.events VALUES (9)", retry_cnt=1)
    assert not cluster.commands


@pytest.mark.parametrize("setting", ["skip_unavailable_shards", "async_insert"])
def test_outage_alias_settings_cannot_weaken_production_policy(outage: Any, setting: str) -> None:
    cluster, configure = outage
    configure(settings={setting: 1})
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="contradicts"):
        sql.execute("ch", "INSERT INTO db.events VALUES (9)")
    assert not cluster.commands


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM db.events SETTINGS skip_unavailable_shards=1",
        "INSERT INTO db.events SETTINGS distributed_foreground_insert=0 VALUES (9)",
        "INSERT INTO db.events SETTINGS async_insert=1 VALUES (9)",
    ],
)
def test_outage_inline_settings_cannot_weaken_delivery(outage: Any, query: str) -> None:
    cluster, _ = outage
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="contradicts"):
        sql.execute("ch", query)
    assert not cluster.commands


def test_outage_partial_insert_select_rejects_unsafe_inline_settings(outage: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterTopologyError, match="contradicts"
    ):
        sql.execute(
            "ch",
            "INSERT INTO db.events SELECT id FROM db.events SETTINGS async_insert=1",
            ch_unavailable_shards="healthy_shards",
        )
    assert not cluster.commands


@pytest.mark.parametrize("key", ["rand()", "id"])
def test_outage_explicit_function_insert_respects_policy(outage: Any, key: str) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    query = f"INSERT INTO FUNCTION cluster('core', 'db', 'events', {key}) VALUES (9)"
    if key == "rand()":
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            sql.execute("ch", query, ch_unavailable_shards="healthy_shards")
        assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (9,)]
    else:
        with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
            sql.ClickHouseClusterUnavailableError, match="rand"
        ):
            sql.execute("ch", query, ch_unavailable_shards="healthy_shards")
        assert not cluster.commands


def test_outage_destructive_ack_loss_preserves_recovery_and_is_not_replayed(outage: Any) -> None:
    cluster, _ = outage
    cluster.failure = TypeError("lost after submission")
    with backend_operation() as state:
        with pytest.raises(sql.AmbiguousSqlReplaceError):
            sql.execute(
                "ch", "TRUNCATE TABLE db.events ON CLUSTER core", retry_cnt=3, timeout_increment=0
            )
        assert state.preserve_recovery is True
        with pytest.raises(sql.ClickHouseClusterUnavailableError, match="retained"):
            sql.execute("ch", "DROP TABLE db.events ON CLUSTER core")
    assert len(cluster.commands) == 1
    settings = cluster.commands[0][2]
    assert settings["distributed_ddl_output_mode"] == "throw"
    assert settings["distributed_ddl_task_timeout"] > 0


def test_outage_notebook_warning_has_red_html(outage: Any, monkeypatch: Any, capsys: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    output = []
    monkeypatch.setitem(sys.modules, "IPython", SimpleNamespace(get_ipython=object))
    monkeypatch.setitem(
        sys.modules, "IPython.display", SimpleNamespace(HTML=str, display=output.append)
    )
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.read("ch", "SELECT id FROM db.events", ch_unavailable_shards="healthy_shards")
    assert output
    assert "color:#c00" in output[0]
    assert "FINISHED" in capsys.readouterr().err


def test_outage_integration_cluster_has_two_independent_shards() -> None:
    root = ET.parse(REPO_ROOT / "integration/clickhouse/config.d/integration-cluster.xml").getroot()
    cluster = root.find("remote_servers/integration_two_shards")
    assert cluster is not None
    shards = cluster.findall("shard")
    assert len(shards) == 2
    assert [len(shard.findall("replica")) for shard in shards] == [1, 1]
    assert {shard.findtext("replica/host") for shard in shards} == {
        "clickhouse",
        "clickhouse-replica",
    }


@pytest.mark.parametrize("write_mode", ["replace", "upsert", "truncate_insert"])
def test_outage_missing_physical_schema_blocks_destructive_load(
    outage: Any, write_mode: str
) -> None:
    cluster, _ = outage
    del cluster.tables[("h2", "db.events_shard")]
    kwargs = (
        {"key_columns": ["id"], "upsert_partition_column": "id"} if write_mode == "upsert" else {}
    )
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterUnavailableError
    ):
        sql.load_df(
            "ch",
            "db.events",
            pd.DataFrame({"id": [9]}),
            write_mode=write_mode,
            table_schema={"id": "UInt64"},
            ch_unavailable_shards="healthy_shards",
            retry_cnt=1,
            **kwargs,
        )
    assert cluster.commands == []
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,)]


def test_outage_coverage_remains_partial_after_empty_schema_repair(outage: Any) -> None:
    cluster, _ = outage
    del cluster.tables[("h2", "db.events_shard")]
    with backend_operation("healthy_shards") as state:
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            sql.read("ch", "SELECT id FROM db.events")
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            sql.execute("ch", "INSERT INTO db.events VALUES (9)")
    assert state.partial is True
    assert ("h2", "db.events_shard") in cluster.tables
    assert cluster.tables[("h2", "db.events_shard")].rows == []


def test_outage_successful_batch_is_not_replayed_after_later_preflight_failure(outage: Any) -> None:
    cluster, _ = outage
    with backend_operation() as state:
        sql.execute("ch", "INSERT INTO db.events VALUES (9)")
        assert state.live_submitted is True
        assert is_non_retryable_sql_error(ConnectionError("next connection unavailable")) is True
        assert state.preserve_recovery is True
    assert len(cluster.commands) == 1


@pytest.mark.parametrize(("driver", "port"), [("http", 8123), ("native", 9015)])
def test_outage_healthy_creation_connects_directly_with_transport_port(
    outage: Any, driver: str, port: int
) -> None:
    cluster, configure = outage
    configure(driver=driver)
    cluster.members[1] = (2, 1, "h2", 9015)
    sql.create_table(
        "ch",
        "db.remote_created",
        table_schema={"id": "UInt64"},
        ch_unavailable_shards="healthy_shards",
    )
    assert ("h2", port) in cluster.connections
    assert (
        cluster.tables[("h1", "db.remote_created_shard")].uuid
        == cluster.tables[("h2", "db.remote_created_shard")].uuid
    )


@pytest.mark.parametrize(
    "query",
    [
        "DROP TABLE db.events ON CLUSTER core",
        "TRUNCATE TABLE db.events ON CLUSTER core",
        "ALTER TABLE db.events_shard ON CLUSTER core DELETE WHERE id = 1",
        "EXCHANGE TABLES db.events AND db.other ON CLUSTER core",
        "RENAME TABLE db.events TO db.other ON CLUSTER core",
    ],
)
def test_outage_missing_schema_blocks_destructive_sql(outage: Any, query: str) -> None:
    cluster, _ = outage
    del cluster.tables[("h2", "db.events_shard")]
    client = sql.get_sql_connection("ch", ch_unavailable_shards="healthy_shards")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterUnavailableError
    ):
        client.command(query)
    assert cluster.commands == []
