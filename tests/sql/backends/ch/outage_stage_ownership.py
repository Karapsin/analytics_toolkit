"""Default production policy must distinguish disposable stages from live data."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.routing import local_sql
from analytics_toolkit.sql.dml.transfer.runtime.retry import is_non_retryable_sql_error
from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests.sql._support.outage_gp import MemoryGreenplum


def test_outage_local_stage_metadata_does_not_inherit_regular_cluster_defaults(outage: Any) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    cluster.alive.remove("h2")
    name = "db.local__stage__metadata"
    with backend_operation():
        client = sql.get_sql_connection("ch")
        client.command(f"CREATE TABLE IF NOT EXISTS {name} (id UInt64) ENGINE=Memory")
        with local_sql(client):
            assert client.query(f"DESCRIBE TABLE {name}").result_rows == [("id", "UInt64")]
            assert client.query(
                "SELECT name, type FROM system.columns "
                "WHERE database='db' AND table='local__stage__metadata'"
            ).result_rows == [("id", "UInt64")]
        assert client.outage_table_exists(name)
        assert not client._session.scopes


@pytest.mark.parametrize("offline", [False, True])
def test_outage_new_target_proof_uses_alias_ddl_defaults_and_requires_ddl_hosts(
    outage: Any, offline: bool
) -> None:
    cluster, _configure = outage
    if offline:
        cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch")
    if offline:
        with pytest.raises(sql.ClickHouseClusterUnavailableError, match="New target"):
            client.outage_assert_new_target(
                "db.new_data", policy=None, cluster=None, only_shard=False
            )
    else:
        client.outage_assert_new_target("db.new_data", policy=None, cluster=None, only_shard=False)
    assert not cluster.commands


@pytest.mark.parametrize("case", ["new", "existing", "missing_database"])
def test_outage_local_target_proof_checks_the_actual_local_scope(outage: Any, case: str) -> None:
    cluster, _configure = outage
    client = sql.get_sql_connection("ch")
    policy = SimpleNamespace(create_distributed_pair=False, shard_on_cluster=None)
    table = "db.events" if case == "existing" else "new_data"
    if case == "missing_database":
        client._config = replace(client._config, database=None)
    if case == "new":
        client.outage_assert_new_target(table, policy=policy, cluster=None, only_shard=True)
    else:
        with pytest.raises(sql.ClickHouseClusterTopologyError):
            client.outage_assert_new_target(table, policy=policy, cluster=None, only_shard=True)
    assert not cluster.commands


@pytest.mark.parametrize("routed", [True, False])
@pytest.mark.parametrize("preexisting", [True, False])
def test_outage_stop_if_not_exists_claims_only_verified_absent_stages(
    outage: Any, routed: bool, preexisting: bool
) -> None:
    cluster, configure = outage
    if not routed:
        configure(cluster_routing=None, ddl_defaults=None)
    name = "db.production__stage__retry"
    if preexisting:
        for host in cluster.hosts:
            cluster.create(host, f"CREATE TABLE {name} (id UInt64) ENGINE=MergeTree ORDER BY id")
            cluster.tables[(host, name)].rows = [(77,)]
    with backend_operation() as state:
        client = sql.get_sql_connection("ch")
        clause = " ON CLUSTER core" if routed else ""
        client.command(
            f"CREATE TABLE IF NOT EXISTS {name}{clause} (id UInt64) ENGINE=MergeTree ORDER BY id"
        )
        assert client.outage_table_exists(name)
        cluster.failure = ConnectionError("stage insert accepted before disconnect")
        with pytest.raises(ConnectionError) as error:
            client.insert(name, [(9,)], column_names=["id"])
        assert state.live_submitted is preexisting
        assert state.preserve_recovery is preexisting
        assert is_non_retryable_sql_error(error.value) is preexisting
        assert cluster.tables[("h1", name)].rows == ([(77,), (9,)] if preexisting else [(9,)])
        if not preexisting:
            cluster.failure = None
            client.command(f"DROP TABLE {name}{clause}")
            assert not any(table == name for _host, table in cluster.tables)
            assert not client.outage_table_exists(name)
        else:
            assert cluster.tables[("h2", name)].rows == [(77,)]


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
@pytest.mark.parametrize("operation", ["load", "transfer"])
def test_outage_per_call_cluster_cannot_reuse_an_orphan_physical_table(
    outage: Any, monkeypatch: Any, policy: str, operation: str
) -> None:
    cluster, configure = outage
    configure(
        cluster_routing=None,
        aliases=("ch", "gp"),
        per_alias={"gp": {"type": "gp", "ddl_defaults": None, "cluster_routing": None}},
        ddl_defaults={
            "staging": {
                "create_distributed_pair": False,
                "shard": {"engine": "Memory", "on_cluster": None},
            }
        },
    )
    for host in cluster.hosts:
        cluster.create(
            host, "CREATE TABLE db.orphan_shard (id UInt64) ENGINE=MergeTree ORDER BY id"
        )
        cluster.tables[(host, "db.orphan_shard")].rows = [(77,)]
    greenplum = MemoryGreenplum()
    greenplum.install(monkeypatch)
    options = {
        "write_mode": "replace",
        "ch_cluster": "core",
        "ch_distributed_table": True,
        "ch_engine": "MergeTree",
        "ch_sharding_key": "rand()",
        "ch_distributed_engine_template": (
            "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
        ),
        "ch_unavailable_shards": policy,
        "retry_cnt": 1,
        "timeout_increment": 0,
    }

    def run() -> None:
        if operation == "load":
            sql.load_df("ch", "db.orphan", pd.DataFrame({"id": [9]}), **options)
        else:
            sql.transfer(
                from_db="gp",
                to_db="ch",
                from_sql="SELECT id FROM db.source",
                to_table="db.orphan",
                ignore_source_staging=True,
                table_schema={"id": "UInt64"},
                full_retry_cnt=1,
                full_timeout_increment=0,
                **options,
            )

    with pytest.warns(DeprecationWarning, match="ch_cluster"), pytest.raises(
        sql.ClickHouseClusterTopologyError, match="not genuinely new"
    ):
        run()
    assert cluster.tables[("h2", "db.orphan_shard")].rows == [(77,)]
    assert cluster.tables[("h1", "db.orphan_shard")].rows == [(77,)]
    assert not any(name == "db.orphan" for _host, name in cluster.tables)
    assert not cluster.commands


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("operation", ["load", "transfer"])
def test_outage_stop_append_uses_replica_failover_without_cluster_ddl(
    outage: Any, monkeypatch: Any, driver: str, operation: str
) -> None:
    cluster, configure = outage
    configure(
        driver=driver,
        host="h2",
        aliases=("ch", "gp"),
        transfer_staging_schema=None,
        per_alias={"gp": {"type": "gp", "ddl_defaults": None, "cluster_routing": None}},
        **(
            {
                "cluster_routing": None,
                "ddl_defaults": {
                    "regular": {
                        "create_distributed_pair": True,
                        "shard": {"engine": "MergeTree", "on_cluster": "core"},
                        "distributed": {
                            "engine_template": (
                                "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                            ),
                            "cluster": "core",
                            "on_cluster": "core",
                            "sharding_key": "rand()",
                        },
                    },
                    "staging": {
                        "create_distributed_pair": False,
                        "shard": {"engine": "Memory", "on_cluster": None},
                    },
                },
            }
            if operation == "transfer"
            else {}
        ),
    )
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    cluster.seed(replicated=True)
    cluster.alive.remove("h1")
    if operation == "load":
        result = sql.load_df(
            "ch",
            "db.events",
            pd.DataFrame({"id": [9]}),
            write_mode="append",
            return_metadata=True,
            retry_cnt=1,
            timeout_increment=0,
        )
        expected = [(2,), (9,)]
    else:
        greenplum = MemoryGreenplum()
        greenplum.install(monkeypatch)
        result = sql.transfer(
            from_db="gp",
            to_db="ch",
            from_sql="SELECT id FROM db.source",
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
        expected = [(2,), (10,), (11,)]
    assert result.metadata.data_complete is True
    assert cluster.tables[("h2", "db.events_shard")].rows == expected
    assert not any("ON CLUSTER" in query for _host, query, _settings in cluster.commands)
