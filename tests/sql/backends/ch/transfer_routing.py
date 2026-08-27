from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from analytics_toolkit.sql.backends.ch.routing import (
    ChClusterRouting,
    route_sql,
    wrap_client,
)
from analytics_toolkit.sql.backends.transfer_stage import (
    is_transfer_stage_identifier,
    match_transfer_stage_identifier,
)
from analytics_toolkit.sql.connection.errors import SqlConfigError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


transfer_api = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")
ch_operations = importlib.import_module("analytics_toolkit.sql.backends.ch.operations")

DESTINATION_HASH = "0123456789abcdef"
TRANSFER_ID = "a" * 32
PRIMARY_STAGE = f"{DESTINATION_HASH}__{TRANSFER_ID}__w00000"
SECONDARY_STAGE = f"{DESTINATION_HASH}__{TRANSFER_ID}__w00001"


class _Result:
    result_rows: ClassVar[list[tuple[str]]] = [(PRIMARY_STAGE,)]


class _Client:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, sql: str, **_kwargs: Any) -> _Result:
        self.queries.append(sql)
        return _Result()


def _connections(
    staging_engine: str = "MergeTree",
    *,
    staging_pair: bool = False,
) -> dict[str, dict[str, object]]:
    return {
        "gp": {
            "type": "gp",
            "host": "gp.example",
            "user": "user",
            "password": "password",
            "database": "dwh",
        },
        "routed": {
            "type": "ch",
            "host": "ch.example",
            "user": "user",
            "password": "password",
            "database": "default",
            "transfer_staging_schema": "stage",
            "cluster_routing": {"cluster": "core", "sharding_key": "rand()"},
            "ddl_defaults": {
                "regular": {
                    "create_distributed_pair": True,
                    "shard": {"engine": "ReplicatedMergeTree", "on_cluster": "core"},
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
                    "create_distributed_pair": staging_pair,
                    "shard": {"engine": staging_engine, "on_cluster": None},
                    "distributed": {
                        "engine_template": (
                            "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                        ),
                        "cluster": "core",
                        "on_cluster": "core",
                        "sharding_key": "rand()",
                    },
                },
            },
        },
    }


def test_transfer_stage_sources_route_through_all_replicas() -> None:
    routed = route_sql(
        f"INSERT INTO stage.{PRIMARY_STAGE} SELECT * FROM stage.{SECONDARY_STAGE}",
        routing=ChClusterRouting("core", "rand()"),
        database="default",
    )

    assert "FUNCTION cluster('core', 'stage'," in routed
    assert "FROM clusterAllReplicas('core', 'stage'," in routed
    assert "FROM cluster('core', 'stage'," not in routed

    ordinary = route_sql(
        "SELECT * FROM analytics.events",
        routing=ChClusterRouting("core", "rand()"),
        database="default",
    )
    assert ordinary == "SELECT * FROM cluster('core', 'analytics', 'events')"


def test_transfer_stage_identifier_contract_is_shared_with_cleanup() -> None:
    collision = f"orders{TRANSFER_ID}__w00000__c_1234abcd"
    match = match_transfer_stage_identifier(collision)

    assert match is not None
    assert match.group("transfer_id") == TRANSFER_ID
    assert is_transfer_stage_identifier(PRIMARY_STAGE)
    assert not is_transfer_stage_identifier("analytics_events")


def test_cluster_routed_stage_discovery_queries_every_replica() -> None:
    raw = _Client()
    connection = wrap_client(
        raw,
        SimpleNamespace(
            cluster_routing=ChClusterRouting("core", "rand()"),
            database="default",
        ),
    )

    names = ch_operations.query_transfer_stage_table_names(
        object(),
        connection,
        connection_key="routed",
        transfer_staging_schema="stage",
        table_pattern=f"{DESTINATION_HASH}__%",
    )

    assert names == [PRIMARY_STAGE]
    assert "SELECT DISTINCT name FROM clusterAllReplicas('core', system, tables)" in raw.queries[0]
    assert f"name LIKE '{DESTINATION_HASH}__%'" in raw.queries[0]


def test_cluster_routed_transfer_uses_merge_tree_stage_on_routing_cluster(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(_connections())

    options = transfer_api.build_transfer_options(
        from_db="gp",
        to_db="routed",
        from_sql="SELECT 1 AS id",
        to_table="analytics.target",
    )

    assert options.staging_ch_policy.shard_engine == "MergeTree"
    assert options.staging_ch_policy.shard_on_cluster == "core"


def test_cluster_routed_transfer_rejects_replicated_stage_before_connecting(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(_connections("ReplicatedMergeTree('/stage/{table}', '{replica}')"))

    with pytest.raises(SqlConfigError, match="requires transfer staging engine MergeTree"):
        transfer_api.build_transfer_options(
            from_db="gp",
            to_db="routed",
            from_sql="SELECT 1 AS id",
            to_table="analytics.target",
        )


def test_cluster_routed_transfer_rejects_invalid_stage_engine(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(_connections("MergeTree("))

    with pytest.raises(SqlConfigError, match="must be a valid engine expression"):
        transfer_api.build_transfer_options(
            from_db="gp",
            to_db="routed",
            from_sql="SELECT 1 AS id",
            to_table="analytics.target",
        )


def test_cluster_routed_transfer_rejects_distributed_stage_pair(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(_connections(staging_pair=True))

    with pytest.raises(SqlConfigError, match="create_distributed_pair=false"):
        transfer_api.build_transfer_options(
            from_db="gp",
            to_db="routed",
            from_sql="SELECT 1 AS id",
            to_table="analytics.target",
        )
