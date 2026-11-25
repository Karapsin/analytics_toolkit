"""Public transfer orchestration keeps partial source work away from destructive targets."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch import created_resources
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from analytics_toolkit.sql.dml.transfer.flow import row_counts
from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests.sql._support.outage import Client
from tests.sql._support.outage_gp import MemoryGreenplum


def _observe_owned_cleanup(monkeypatch: Any, cluster: Any) -> list[Any]:
    receipts = []
    original = created_resources.drop_created_resource

    def drop(adapter: Any, connection: Any, resource: Any) -> None:
        certificate = resource.certificate
        assert isinstance(certificate, created_resources.CreatedIdentity)
        assert resource.scope.kind in {"host", "local"}
        assert resource.scope.endpoint in {None, ("h1", 9000)}
        assert certificate.resource.scope is None
        name = ".".join(TableIdentifier.parse(resource.name, "ch").parts)
        current = cluster.tables[("h1", name)]
        assert certificate.uuid == UUID(current.uuid)
        assert certificate.resource.table_identities == ((1, certificate.uuid),)
        receipts.append(resource)
        original(adapter, connection, resource)

    monkeypatch.setattr(created_resources, "drop_created_resource", drop)
    return receipts


@pytest.mark.parametrize("routed", [False, True], ids=["local", "all-replicas"])
@pytest.mark.parametrize("empty", [False, True], ids=["rows", "empty"])
def test_outage_snapshot_group_counts_use_actual_columns_and_values(
    outage: Any, routed: bool, empty: bool
) -> None:
    cluster, configure = outage
    configure(cluster_routing=None, ddl_defaults=None)
    for host in cluster.hosts:
        cluster.create(
            host,
            "CREATE TABLE db.snapshot_groups (id UInt64, category String, bucket UInt64) "
            "ENGINE=Memory",
        )
    if not empty:
        cluster.tables[("h1", "db.snapshot_groups")].rows = [
            (4, "red", 1),
            (5, "blue", 1),
            (6, "red", 1),
            (7, "red", 2),
        ]
        cluster.tables[("h2", "db.snapshot_groups")].rows = [(8, "blue", 1)]
    source = (
        "clusterAllReplicas('core', 'db', 'snapshot_groups')" if routed else "db.snapshot_groups"
    )
    result = sql.read(
        "ch", f"SELECT category, bucket, COUNT(*) AS n FROM {source} GROUP BY category, bucket"
    )
    expected = [] if empty else [("blue", 1, 2 if routed else 1), ("red", 1, 2), ("red", 2, 1)]
    assert list(result.columns) == ["category", "bucket", "n"]
    assert sorted(result.itertuples(index=False, name=None)) == expected


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
    receipts = _observe_owned_cleanup(monkeypatch, cluster)
    source_snapshots = []
    source_changed = False
    original_query = Client.query

    def query(client: Any, statement: str, **kwargs: Any) -> Any:
        nonlocal source_changed
        result = original_query(client, statement, **kwargs)
        if direction == "from_ch" and "GROUP BY" in statement and "__source" in statement:
            source_snapshots.extend(
                name for host, name in cluster.tables if host == "h1" and name.endswith("__source")
            )
            cluster.tables[("h1", "db.events_shard")].rows = [(7,)]
            source_changed = True
        return result

    monkeypatch.setattr(Client, "query", query)
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
            ignore_source_staging=direction != "from_ch",
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
        assert source_changed
        assert greenplum.tables["db.target"].id.tolist() == [99, 1]
    else:
        assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (10,), (11,)]
    assert receipts if direction == "to_ch" else source_snapshots
    assert all(
        ("h1", ".".join(TableIdentifier.parse(resource.name, "ch").parts)) not in cluster.tables
        for resource in receipts
    )
    assert all(("h1", name) not in cluster.tables for name in source_snapshots)


def test_outage_transfer_keeps_independent_alias_defaults_and_completeness(
    outage: Any, monkeypatch: Any
) -> None:
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
    receipts = _observe_owned_cleanup(monkeypatch, cluster)
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
    assert receipts
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


def test_outage_unstaged_source_same_count_change_cannot_validate_a_live_transfer(
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
    original = row_counts._require_partial_source_snapshot
    changed = False

    def require_snapshot(options: Any) -> None:
        nonlocal changed
        if not changed:
            cluster.tables[("h1", "db.events_shard")].rows = [(7,)]
            changed = True
        original(options)

    monkeypatch.setattr(row_counts, "_require_partial_source_snapshot", require_snapshot)
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterUnavailableError, match="snapshot"
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
    assert greenplum.tables["db.target"].id.tolist() == [99]
    assert changed


@pytest.mark.parametrize("staging", [None, "db"])
def test_outage_partial_source_cannot_disable_snapshot_validation(staging: Any) -> None:
    options = SimpleNamespace(
        from_db_key="ch",
        source_transfer_staging_schema=staging,
        validate_row_count=False,
    )
    with backend_operation() as state:
        state.incomplete_aliases.add("ch")
        with pytest.raises(sql.ClickHouseClusterUnavailableError, match="snapshot"):
            row_counts.prepare_row_count_validated_options(
                options=options,
                connection_refs=None,
                stage_state=None,
            )


def test_outage_late_partial_source_is_blocked_even_when_counting_is_disabled(
    outage: Any, monkeypatch: Any
) -> None:
    cluster, configure = outage
    configure(
        aliases=("ch", "gp"),
        transfer_staging_schema="db",
        ch_unavailable_shards="healthy_shards",
        per_alias={"gp": {"type": "gp", "ddl_defaults": None, "cluster_routing": None}},
    )
    greenplum = MemoryGreenplum()
    greenplum.install(monkeypatch)
    original = row_counts._require_partial_source_snapshot

    def require_snapshot(options: Any) -> None:
        original(options)
        cluster.alive.discard("h2")

    monkeypatch.setattr(row_counts, "_require_partial_source_snapshot", require_snapshot)
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterUnavailableError, match="snapshot"
    ):
        sql.transfer(
            from_db="ch",
            to_db="gp",
            from_sql="SELECT id FROM db.events",
            to_table="db.target",
            write_mode="append",
            ignore_source_staging=True,
            validate_row_count=False,
            table_schema={"id": "bigint"},
            retry_cnt=1,
            full_retry_cnt=1,
            timeout_increment=0,
            full_timeout_increment=0,
        )
    assert greenplum.tables["db.target"].id.tolist() == [99]
