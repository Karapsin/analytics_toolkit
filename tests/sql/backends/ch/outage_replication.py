"""Replica identity proofs precede every healthy-only CREATE, including recovery."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.outage_replication import _expand


def _replicated(outage: Any, engine: str = "ReplicatedMergeTree") -> Any:
    cluster, _ = outage
    cluster.tables.clear()
    cluster.seed(replicated=True)
    for host in cluster.hosts:
        obj = cluster.tables[(host, "db.events_shard")]
        obj.ddl = obj.ddl.replace(obj.engine_full, engine)
        obj.engine_full = engine
    return cluster


@pytest.mark.parametrize(
    "engine",
    [
        "ReplicatedMergeTree",
        "ReplicatedMergeTree('/tables/{uuid}/{shard}', '{replica}')",
    ],
)
def test_outage_replica_recovery_uses_validated_destination_macros(
    outage: Any, engine: str
) -> None:
    cluster = _replicated(outage, engine)
    identity = cluster.tables[("h1", "db.events_shard")].uuid
    del cluster.tables[("h2", "db.events_shard")]
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.execute(
            "ch", "INSERT INTO db.events VALUES (9)", ch_unavailable_shards="healthy_shards"
        )
    assert cluster.tables[("h2", "db.events_shard")].uuid == identity
    assert cluster.tables[("h2", "db.events_shard")].rows == []
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (9,)]


def test_outage_fixed_keeper_path_cannot_join_an_independent_recovered_shard(outage: Any) -> None:
    cluster = _replicated(outage, "ReplicatedMergeTree('/fixed', '{replica}')")
    del cluster.tables[("h2", "db.events_shard")]
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterTopologyError, match="shard-separated"
    ):
        sql.execute(
            "ch", "INSERT INTO db.events VALUES (9)", ch_unavailable_shards="healthy_shards"
        )
    assert not cluster.commands
    assert ("h2", "db.events_shard") not in cluster.tables


def test_outage_fixed_replica_name_is_rejected_before_recovery_create(outage: Any) -> None:
    cluster = _replicated(outage, "ReplicatedMergeTree('/tables/{uuid}/{shard}', 'fixed')")
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    del cluster.tables[("h2", "db.events_shard")]
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterTopologyError, match="identity"
    ):
        sql.execute(
            "ch", "INSERT INTO db.events VALUES (9)", ch_unavailable_shards="healthy_shards"
        )
    assert not cluster.commands


def test_outage_all_destination_macros_checked_before_first_create(outage: Any) -> None:
    cluster, _ = outage
    cluster.host_macros["h2"] = {"shard": "1", "replica": "h2"}
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="identity"):
        sql.create_table(
            "ch",
            "db.replicated_new",
            table_schema={"id": "UInt64"},
            ch_engine="ReplicatedMergeTree",
            ch_unavailable_shards="healthy_shards",
        )
    assert not cluster.commands
    assert not any("replicated_new" in table for _, table in cluster.tables)


def test_outage_new_replica_group_requires_uuid_isolation(outage: Any) -> None:
    cluster, _ = outage
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="uuid"):
        sql.create_table(
            "ch",
            "db.replicated_new",
            table_schema={"id": "UInt64"},
            ch_engine="ReplicatedMergeTree('/fixed/{shard}', '{replica}')",
            ch_unavailable_shards="healthy_shards",
        )
    assert not cluster.commands


def test_outage_uninspectable_server_defaults_fail_before_creation(
    outage: Any, monkeypatch: Any
) -> None:
    cluster, _ = outage
    cluster.replica_defaults.clear()
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="defaults"):
        sql.create_table(
            "ch",
            "db.replicated_new",
            table_schema={"id": "UInt64"},
            ch_engine="ReplicatedMergeTree",
            ch_unavailable_shards="healthy_shards",
        )
    assert not cluster.commands


@pytest.mark.parametrize("template", ["{unknown}", "{uuid!r}", "{uuid:>3}", "{unclosed"])
def test_outage_replica_macro_expansion_is_fail_closed(template: str) -> None:
    with pytest.raises(sql.ClickHouseClusterTopologyError):
        _expand(template, {"uuid": "id"})


def test_outage_replica_macro_expansion_preserves_literal_path() -> None:
    assert _expand("/tables/{uuid}/{shard}", {"uuid": "id", "shard": "2"}) == "/tables/id/2"


def test_outage_new_replicated_creation_proves_all_hosts_before_schema(outage: Any) -> None:
    cluster, _ = outage
    sql.create_table(
        "ch",
        "db.replica_good",
        table_schema={"id": "UInt64"},
        ch_engine="ReplicatedMergeTree",
        ch_unavailable_shards="healthy_shards",
    )
    assert (
        cluster.tables[("h1", "db.replica_good_shard")].uuid
        == cluster.tables[("h2", "db.replica_good_shard")].uuid
    )
    assert cluster.tables[("h2", "db.replica_good_shard")].rows == []


def test_outage_partial_replica_arguments_fail_before_any_create(outage: Any) -> None:
    cluster, _ = outage
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="both"):
        sql.create_table(
            "ch",
            "db.bad_replica",
            table_schema={"id": "UInt64"},
            ch_engine="ReplicatedMergeTree('/broken')",
            ch_unavailable_shards="healthy_shards",
        )
    assert not cluster.commands


def test_outage_existing_replica_identity_must_match_predicted_template(outage: Any) -> None:
    cluster = _replicated(outage)
    del cluster.tables[("h2", "db.events_shard")]
    cluster.host_macros["h1"] = {"shard": "99", "replica": "h1"}
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterTopologyError, match="disagree"
    ):
        sql.execute(
            "ch", "INSERT INTO db.events VALUES (9)", ch_unavailable_shards="healthy_shards"
        )
    assert not cluster.commands


def test_outage_ordinary_database_cannot_use_unknown_uuid_identity(outage: Any) -> None:
    cluster, _ = outage
    cluster.database_engine = "Ordinary"
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="identity"):
        sql.create_table(
            "ch",
            "db.bad_replica",
            table_schema={"id": "UInt64"},
            ch_engine="ReplicatedMergeTree",
            ch_unavailable_shards="healthy_shards",
        )
    assert not cluster.commands


@pytest.mark.parametrize("name", ["database", "table"])
def test_outage_explicit_server_macros_override_builtin_names_before_creation(
    outage: Any, name: str
) -> None:
    cluster, _ = outage
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.host_macros = {
        "h1": {"shard": "1", "replica": "h1", name: "one"},
        "h2": {"shard": "1", "replica": "h2", name: "two"},
    }
    engine = "ReplicatedMergeTree('/tables/{uuid}/{" + name + "}/{shard}', '{replica}')"
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="identity"):
        sql.create_table(
            "ch",
            "db.bad_override",
            table_schema={"id": "UInt64"},
            ch_engine=engine,
            ch_unavailable_shards="healthy_shards",
        )
    assert not cluster.commands


def test_outage_server_uuid_macro_cannot_defeat_new_group_isolation(outage: Any) -> None:
    cluster, _ = outage
    cluster.host_macros["h1"] = {"shard": "1", "replica": "h1", "uuid": "shared-old-group"}
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="UUID"):
        sql.create_table(
            "ch",
            "db.bad_override",
            table_schema={"id": "UInt64"},
            ch_engine="ReplicatedMergeTree",
            ch_unavailable_shards="healthy_shards",
        )
    assert not cluster.commands


@pytest.mark.parametrize(
    ("template", "macros"),
    [
        ("/tables/{{uuid}}", {"uuid": "new"}),
        ("/tables/{shard}", {"shard": "{region}", "region": "one"}),
    ],
)
def test_outage_nested_or_escaped_replica_macros_are_not_assumed_safe(
    template: str, macros: Any
) -> None:
    with pytest.raises(sql.ClickHouseClusterTopologyError):
        _expand(template, macros)
