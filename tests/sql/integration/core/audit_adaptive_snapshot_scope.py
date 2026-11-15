"""A keyed native checkpoint cannot certify a changing healthy-source scope."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

import analytics_toolkit.general as general_module
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.transfer.flow import staged_attempt
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import resolve_destination_identity

from tests.sql.integration._support.backends import integration_table
from tests.sql.integration._support.reconfiguration import (
    create_owned,
    label,
    literal,
    quoted,
    resource_name,
)

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]

_FULL = "integration_two_shards"
_ARCHIVE = "integration_archive_cluster"
_DATABASE = "integration"


def _source(registry: ResourceRegistry) -> tuple[str, str]:
    relation = resource_name("adaptive_scope")
    table, physical = quoted(_DATABASE, relation), quoted(_DATABASE, relation + "_shard")
    scope = quoted(_FULL)
    create_owned(
        registry,
        f"CREATE TABLE {physical} ON CLUSTER {scope} (id Int256) ENGINE=MergeTree ORDER BY id",
        f"DROP TABLE IF EXISTS {physical} ON CLUSTER {scope} SYNC",
    )
    create_owned(
        registry,
        f"CREATE TABLE {table} ON CLUSTER {scope} (id Int256) "
        f"ENGINE=Distributed({literal(_FULL)}, {literal(_DATABASE)}, "
        f"{literal(relation + '_shard')}, rand())",
        f"DROP TABLE IF EXISTS {table} ON CLUSTER {scope} SYNC",
    )
    # Both independent hosts start with both keys. These are run-owned tables.
    for cluster in ("integration_cluster", _ARCHIVE):
        sql.execute(
            "ch",
            f"INSERT INTO FUNCTION cluster({literal(cluster)}, {literal(_DATABASE)}, "
            f"{literal(relation + '_shard')}, rand()) "
            "SELECT toInt256(arrayJoin([1, 2])) AS id",
            retry_cnt=1,
            query_label=label("adaptive-scope-seed"),
        )
    return table, physical


def _stage_footprint(prefix: str) -> list[tuple[object, ...]]:
    frame = sql.read(
        "ch",
        "SELECT database, name, toString(uuid) AS identity "
        f"FROM clusterAllReplicas({literal(_FULL)}, system, tables) "
        f"WHERE database={literal(_DATABASE)} AND startsWith(name, {literal(prefix)}) "
        "ORDER BY database, name, identity",
        retry_cnt=1,
        query_label=label("adaptive-scope-stage-inventory"),
    )
    return list(frame.itertuples(index=False, name=None))


@pytest.mark.sql_scenario("audit.transfer.clickhouse_adaptive_snapshot_scope")
def test_changed_keyed_native_source_scope_cannot_publish_a_certified_checkpoint(
    resource_registry: ResourceRegistry,
    integration_connections,
    write_sql_connections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, physical = _source(resource_registry)
    target = resource_registry.table("gp", integration_table("gp", "adaptive_scope_target"))
    # Existing BIGINT requires complete-value validation of the declared Int256
    # input. A new Greenplum NUMERIC(78) would already cover its full domain.
    sql.create_table("gp", target, table_schema={"id": "BIGINT"}, retry_cnt=1)
    sql.execute("gp", f"INSERT INTO {target} (id) VALUES (99)", retry_cnt=1)
    prefix = resolve_destination_identity(target, "gp").hash_prefix
    before = _stage_footprint(prefix)

    # Copy only the disposable stack's generated configuration.
    connections = deepcopy(integration_connections)
    alias = resource_name("adaptive_scope_alias")
    connection = deepcopy(connections["ch"])
    connection["cluster_routing"] = {"cluster": _FULL}
    connection["transfer_staging_schema"] = _DATABASE
    connection["ddl_defaults"]["staging"] = {
        "create_distributed_pair": False,
        "shard": {"engine": "MergeTree", "on_cluster": _FULL},
    }
    connections[alias] = connection
    general_module.set_connections_path(write_sql_connections(connections))

    original = staged_attempt.execute_transfer_materialization
    injected = False

    def remove_source_host_before_second_key(*args, **kwargs):
        nonlocal injected
        assert not injected
        sql.execute(
            "ch",
            f"DROP TABLE {physical} ON CLUSTER {quoted(_ARCHIVE)} SYNC",
            retry_cnt=1,
            query_label=label("adaptive-scope-owned-source-fault"),
        )
        injected = True
        return original(*args, **kwargs)

    # This retained internal seam only injects a real owned-source fault. The
    # original source append, routing, counts and validation still execute.
    monkeypatch.setattr(
        staged_attempt, "execute_transfer_materialization", remove_source_host_before_second_key
    )
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        ValueError, match="availability scope changed"
    ):
        sql.transfer(
            alias,
            "gp",
            from_sql=f"SELECT id FROM {source} WHERE {{id}}",
            to_table=target,
            write_mode="append",
            transfer_keys="id",
            transfer_key_values=[1, 2],
            ignore_source_staging=False,
            validate_row_count=True,
            ch_unavailable_shards="healthy_shards",
            batch_size=10,
            min_batch_size=1,
            max_batch_size=10,
            adaptive_batch_size=False,
            read_concurrency=1,
            write_concurrency=1,
            retry_cnt=1,
            full_retry_cnt=1,
            timeout_increment=0,
            full_timeout_increment=0,
            query_label=label("adaptive-scope-generation"),
        )
    assert injected
    assert sql.read("gp", f"SELECT id FROM {target} ORDER BY id", retry_cnt=1)["id"].tolist() == [
        99
    ]
    assert _stage_footprint(prefix) == before
