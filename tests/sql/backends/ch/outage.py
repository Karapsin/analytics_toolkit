from __future__ import annotations

import inspect
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.routing import _RoutingClickHouse
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.execution.backend_options import backend_operation
from sqlglot import exp, parse_one


def test_production_default_rejects_missing_shard_before_mutation(outage: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    assert get_connection_config("ch").ch_unavailable_shards == "stop"
    for query in (
        "SELECT id FROM db.events",
        "INSERT INTO db.events VALUES (9)",
        "DROP TABLE db.events ON CLUSTER core",
    ):
        operation = sql.read if query.startswith("SELECT") else sql.execute
        with pytest.raises(sql.ClickHouseClusterUnavailableError):
            operation("ch", query, retry_cnt=3, timeout_increment=0)
    assert cluster.commands == []


def test_ad_hoc_read_warns_and_reports_partial_coverage(outage: Any, capsys: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        result = sql.read(
            "ch",
            "SELECT id FROM db.events",
            ch_unavailable_shards="healthy_shards",
            return_metadata=True,
            print_queries=False,
        )
    assert result.data.id.tolist() == [1]
    assert result.metadata.data_complete is False
    assert result.metadata.ch_cluster_status[-1]["selected_shards"] == [1]
    assert "DEGRADED MODE" in capsys.readouterr().err
    assert cluster.commands == []


def test_connection_default_and_per_call_override(outage: Any) -> None:
    cluster, configure = outage
    configure(ch_unavailable_shards="healthy_shards")
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        assert sql.read("ch", "SELECT id FROM db.events").id.tolist() == [1]
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.read("ch", "SELECT id FROM db.events", ch_unavailable_shards="stop")


@pytest.mark.parametrize("driver", ["http", "native"])
def test_new_target_creation_is_local_to_healthy_hosts(outage: Any, driver: str) -> None:
    cluster, configure = outage
    configure(driver=driver)
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.create_table(
            "ch",
            "db.new_events",
            table_schema={"id": "UInt64"},
            ch_unavailable_shards="healthy_shards",
        )
    assert ("h1", "db.new_events_shard") in cluster.tables
    assert ("h1", "db.new_events") in cluster.tables
    assert ("h2", "db.new_events_shard") not in cluster.tables
    assert ("h2", "db.new_events") not in cluster.tables
    assert all(
        host == "h1" and "ON CLUSTER" not in query.upper() for host, query, _ in cluster.commands
    )


def test_new_target_stop_does_not_submit_any_ddl(outage: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.create_table(
            "ch", "db.new_events", table_schema={"id": "UInt64"}, retry_cnt=3, timeout_increment=0
        )
    assert not cluster.commands


@pytest.mark.parametrize("driver", ["http", "native"])
def test_all_rows_are_accepted_on_surviving_shard(outage: Any, driver: str) -> None:
    cluster, configure = outage
    configure(driver=driver)
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        result = sql.execute(
            "ch",
            "INSERT INTO db.events VALUES (9), (10)",
            ch_unavailable_shards="healthy_shards",
            return_metadata=True,
        )
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (9,), (10,)]
    assert cluster.tables[("h2", "db.events_shard")].rows == [(2,)]
    assert cluster.commands[-1][2]["insert_shard_id"] == 1
    assert cluster.commands[-1][2]["skip_unavailable_shards"] == 0
    assert result.metadata.data_complete is False


@pytest.mark.parametrize("driver", ["http", "native"])
def test_binary_delivery_uses_same_policy(outage: Any, driver: str) -> None:
    cluster, configure = outage
    configure(driver=driver)
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch", ch_unavailable_shards="healthy_shards")
    assert isinstance(client, OutageClient)
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        client.insert_df("db.events", pd.DataFrame({"id": [9, 10]}), column_names=["id"])
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (9,), (10,)]


@pytest.mark.parametrize("key", ["cityHash64(id)", "rand(42)", "id"])
def test_degraded_writes_reject_non_random_effective_key(outage: Any, key: str) -> None:
    cluster, _ = outage
    cluster.tables.clear()
    cluster.seed(key=key)
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterUnavailableError, match="rand"
    ):
        sql.execute(
            "ch", "INSERT INTO db.events VALUES (9)", ch_unavailable_shards="healthy_shards"
        )
    assert not cluster.commands


@pytest.mark.parametrize(
    "failure", [ConnectionError("lost acknowledgment"), TypeError("driver failed after acceptance")]
)
def test_uncertain_live_insert_is_never_replayed(outage: Any, failure: Exception) -> None:
    cluster, _ = outage
    cluster.failure = failure
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(type(failure)):
        sql.execute(
            "ch",
            "INSERT INTO db.events VALUES (9)",
            ch_unavailable_shards="healthy_shards",
            retry_cnt=3,
            timeout_increment=0,
        )
    assert cluster.tables[("h1", "db.events_shard")].rows.count((9,)) == 1
    assert len(cluster.commands) == 1


def test_recovery_read_leaves_schema_absent_write_adds_empty_schema(outage: Any) -> None:
    cluster, _ = outage
    for name in ("db.events", "db.events_shard"):
        del cluster.tables[("h2", name)]
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        assert sql.read(
            "ch", "SELECT id FROM db.events", ch_unavailable_shards="healthy_shards"
        ).id.tolist() == [1]
    assert not cluster.commands
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.read("ch", "SELECT id FROM db.events")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.execute(
            "ch", "INSERT INTO db.events VALUES (9)", ch_unavailable_shards="healthy_shards"
        )
    assert cluster.tables[("h2", "db.events_shard")].rows == []
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (9,)]
    assert all(
        parse_one(query, read=_RoutingClickHouse).find(exp.OnCluster) is None
        for _, query, _ in cluster.commands
    )
    assert sql.read("ch", "SELECT id FROM db.events").id.tolist() == [1, 9]


@pytest.mark.parametrize(
    "command",
    [
        "TRUNCATE TABLE db.events ON CLUSTER core",
        "ALTER TABLE db.events_shard ON CLUSTER core DROP PARTITION 1",
        "DROP TABLE db.events ON CLUSTER core",
    ],
)
def test_degraded_destructive_commands_never_reach_server(outage: Any, command: str) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterUnavailableError
    ):
        sql.execute("ch", command, ch_unavailable_shards="healthy_shards")
    assert cluster.commands == []


@pytest.mark.parametrize("engine", ["Replicated", "Shared", "Unknown"])
def test_database_ddl_replication_cannot_expand_selected_scope(outage: Any, engine: str) -> None:
    cluster, _ = outage
    cluster.database_engine = engine
    cluster.alive.remove("h2")
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="local database DDL"):
        sql.create_table(
            "ch",
            "db.new_events",
            table_schema={"id": "UInt64"},
            ch_unavailable_shards="healthy_shards",
            retry_cnt=1,
        )
    assert not cluster.commands


@pytest.mark.parametrize("policy", [False, "skip", 1])
def test_invalid_policy_rejected_even_without_database_access(policy: Any) -> None:
    with pytest.raises(ValueError, match="ch_unavailable_shards"):
        sql.read("missing", "SELECT 1", ch_unavailable_shards=policy)


def test_policy_keyword_is_exposed_consistently() -> None:
    for name in ("read", "execute", "create_table", "load_df", "transfer", "insert", "drop_tables"):
        parameter = inspect.signature(getattr(sql, name)).parameters["ch_unavailable_shards"]
        assert parameter.default is None
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_metadata_conflicts_and_probe_permissions_do_not_degrade(outage: Any) -> None:
    cluster, _ = outage
    cluster.tables[
        ("h2", "db.events_shard")
    ].ddl = "CREATE TABLE db.events_shard (id String) ENGINE=MergeTree ORDER BY id"
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="Conflicting"):
        sql.read(
            "ch", "SELECT id FROM db.events", ch_unavailable_shards="healthy_shards", retry_cnt=1
        )
    cluster.probe_failure = PermissionError("metadata permission denied")
    with pytest.raises(PermissionError):
        sql.read(
            "ch", "SELECT id FROM db.events", ch_unavailable_shards="healthy_shards", retry_cnt=1
        )
    assert not cluster.commands


def test_zero_healthy_hosts_fails_closed(outage: Any) -> None:
    cluster, _ = outage
    # Entry point remains available while all remote cluster probes fail.
    cluster.members = [(1, 1, "remote1", 9000), (2, 1, "remote2", 9000)]
    cluster.hosts = ["remote1", "remote2"]
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="No hosts"):
        sql.read("ch", "SELECT id FROM db.events", ch_unavailable_shards="healthy_shards")


def test_operation_context_is_reset_after_failure(outage: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    with backend_operation("healthy_shards"), pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.read("ch", "SELECT id FROM db.events")
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.read("ch", "SELECT id FROM db.events")


def test_partial_insert_select_relays_source_before_acknowledged_delivery(outage: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.create_table(
            "ch", "db.copied", table_schema={"id": "UInt64"}, ch_unavailable_shards="healthy_shards"
        )
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.execute(
            "ch",
            "INSERT INTO db.copied SELECT id FROM db.events",
            ch_unavailable_shards="healthy_shards",
        )
    assert cluster.tables[("h1", "db.copied_shard")].rows == [(1,)]
    assert all("SELECT" not in query.upper() for _, query, _ in cluster.commands)
    assert cluster.commands[-1][2]["skip_unavailable_shards"] == 0


def test_dataframe_append_uses_policy_through_creation_and_delivery(outage: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        result = sql.load_df(
            "ch",
            "db.events",
            pd.DataFrame({"id": [9, 10]}),
            append=True,
            table_schema={"id": "UInt64"},
            ch_unavailable_shards="healthy_shards",
            return_metadata=True,
            retry_cnt=1,
            timeout_increment=0,
        )
    assert result.inserted_rows == 2
    assert result.metadata.data_complete is False
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (9,), (10,)]


def test_dataframe_append_does_not_replay_wrapped_ambiguous_error(outage: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    cluster.failure = ConnectionError("lost after acceptance")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        Exception, match="Ambiguous stage insert"
    ):
        sql.load_df(
            "ch",
            "db.events",
            pd.DataFrame({"id": [9]}),
            append=True,
            table_schema={"id": "UInt64"},
            ch_unavailable_shards="healthy_shards",
            retry_cnt=3,
            timeout_increment=0,
        )
    assert cluster.tables[("h1", "db.events_shard")].rows.count((9,)) == 1


@pytest.mark.parametrize("mode", [None, "truncate_insert"])
def test_outage_new_dataframe_target_skips_destructive_preparation(outage: Any, mode: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    options = {} if mode is None else {"write_mode": mode}
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        result = sql.load_df(
            "ch",
            "db.new_data",
            pd.DataFrame({"id": [9, 10]}),
            table_schema={"id": "UInt64"},
            ch_unavailable_shards="healthy_shards",
            return_metadata=True,
            retry_cnt=1,
            **options,
        )
    assert result.inserted_rows == 2
    assert result.metadata.data_complete is False
    assert cluster.tables[("h1", "db.new_data_shard")].rows == [(9,), (10,)]
    assert ("h2", "db.new_data_shard") not in cluster.tables
    assert all(
        "DROP" not in command and "TRUNCATE" not in command for _, command, _ in cluster.commands
    )


@pytest.mark.parametrize("write_mode", ["replace", "upsert", "truncate_insert"])
def test_existing_dataframe_destructive_mode_rejected_before_staging(
    outage: Any, write_mode: str
) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
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
            ch_unavailable_shards="healthy_shards",
            retry_cnt=1,
            **kwargs,
        )
    assert not cluster.commands
