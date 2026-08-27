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
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import resolve_internal_columns

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


transfer_api = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")
ch_operations = importlib.import_module("analytics_toolkit.sql.backends.ch.operations")
source_snapshot = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.source_snapshot")
transfer_stage = importlib.import_module("analytics_toolkit.sql.backends.transfer_stage")

DESTINATION_HASH = "0123456789abcdef"
TRANSFER_ID = "a" * 32
PRIMARY_STAGE = f"{DESTINATION_HASH}__{TRANSFER_ID}__w00000"
SECONDARY_STAGE = f"{DESTINATION_HASH}__{TRANSFER_ID}__w00001"
SOURCE_STAGE = f"{DESTINATION_HASH}__{TRANSFER_ID}__s00000"


class _Result:
    result_rows: ClassVar[list[tuple[str]]] = [(PRIMARY_STAGE,)]


class _Client:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, sql: str, **_kwargs: Any) -> _Result:
        self.queries.append(sql)
        return _Result()


class _CommandClient:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, sql: str, **_kwargs: Any) -> None:
        self.commands.append(sql)


def _connections(
    staging_engine: str = "MergeTree",
    *,
    staging_pair: bool = False,
    wait_policy: str | None = None,
) -> dict[str, dict[str, object]]:
    connections: dict[str, dict[str, object]] = {
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
    if wait_policy is not None:
        connections["routed"]["ch_ddl_wait_policy"] = wait_policy
    return connections


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

    source_stage_read = route_sql(
        f"SELECT * FROM stage.{SOURCE_STAGE}",
        routing=ChClusterRouting("core", "rand()"),
        database="default",
    )
    assert "FROM clusterAllReplicas('core', 'stage'," in source_stage_read


def test_cluster_routed_source_snapshot_creates_waits_and_populates_once(
    monkeypatch: Any,
) -> None:
    raw = _CommandClient()
    connection = wrap_client(
        raw,
        SimpleNamespace(
            cluster_routing=ChClusterRouting("core", "rand()"),
            database="default",
        ),
    )
    events: list[str] = []
    policy = SimpleNamespace(shard_on_cluster="core", ddl_wait_policy="wait_shard")

    class _Adapter:
        @staticmethod
        def quote_identifier(identifier: str) -> str:
            return f"`{identifier}`"

        @staticmethod
        def execute_command(current_connection: Any, sql: str) -> None:
            events.append("command")
            current_connection.command(sql)

        @staticmethod
        def after_create_table(
            _connection: Any,
            table_name: str,
            **kwargs: Any,
        ) -> None:
            assert table_name == f"stage.{SOURCE_STAGE}"
            assert kwargs == {
                "ch_only_shard": True,
                "ch_creation_policy": policy,
            }
            events.append("ready")

    monkeypatch.setattr(source_snapshot, "get_backend_adapter", lambda _backend: _Adapter())
    internal = resolve_internal_columns(["id"], "ch")

    plan = source_snapshot.execute_source_snapshot_materialization(
        backend="ch",
        connection=connection,
        snapshot_table=f"stage.{SOURCE_STAGE}",
        snapshot_select_sql="SELECT id, 0 AS slice_id, 1 AS ordinal FROM analytics.source",
        internal_columns=internal,
        source_staging_ch_policy=policy,
    )

    assert events == ["command", "ready", "command"]
    assert plan.populate_sql is not None
    assert len(raw.commands) == 2
    assert "ON CLUSTER 'core'" in raw.commands[0]
    assert " EMPTY AS SELECT " in raw.commands[0]
    assert "FROM cluster('core', 'analytics', 'source')" in raw.commands[0]
    assert "INSERT INTO FUNCTION cluster('core', 'stage'," in raw.commands[1]
    assert "FROM cluster('core', 'analytics', 'source')" in raw.commands[1]


def test_source_snapshot_routing_helpers_reject_non_clickhouse_backend() -> None:
    assert not transfer_stage.is_cluster_routed_source_snapshot("gp", object())

    with pytest.raises(KeyError, match="gp"):
        transfer_stage.build_cluster_routed_source_snapshot_sqls(
            "gp",
            "stage.snapshot",
            "SELECT 1",
            "slice_id",
            "ordinal",
        )


def test_cluster_routed_source_snapshot_requires_staging_policy(monkeypatch: Any) -> None:
    connection = wrap_client(
        _CommandClient(),
        SimpleNamespace(
            cluster_routing=ChClusterRouting("core", "rand()"),
            database="default",
        ),
    )

    class _Adapter:
        @staticmethod
        def quote_identifier(identifier: str) -> str:
            return f"`{identifier}`"

        @staticmethod
        def execute_command(current_connection: Any, sql: str) -> None:
            current_connection.command(sql)

    monkeypatch.setattr(source_snapshot, "get_backend_adapter", lambda _backend: _Adapter())

    with pytest.raises(RuntimeError, match="staging policy is missing"):
        source_snapshot.execute_source_snapshot_materialization(
            backend="ch",
            connection=connection,
            snapshot_table=f"stage.{SOURCE_STAGE}",
            snapshot_select_sql="SELECT id, 0 AS slice_id, 1 AS ordinal FROM analytics.source",
            internal_columns=resolve_internal_columns(["id"], "ch"),
        )


@pytest.mark.parametrize("run_post_create_sqls", [False, True])
def test_non_routed_source_snapshot_controls_post_create_commands(
    monkeypatch: Any,
    run_post_create_sqls: bool,
) -> None:
    commands: list[str] = []

    class _Adapter:
        @staticmethod
        def quote_identifier(identifier: str) -> str:
            return f'"{identifier}"'

        @staticmethod
        def execute_command(_connection: Any, sql: str) -> None:
            commands.append(sql)

    monkeypatch.setattr(source_snapshot, "get_backend_adapter", lambda _backend: _Adapter())

    plan = source_snapshot.execute_source_snapshot_materialization(
        backend="gp",
        connection=object(),
        snapshot_table="stage.snapshot",
        snapshot_select_sql="SELECT id, 0 AS slice_id, 1 AS ordinal FROM source_table",
        internal_columns=resolve_internal_columns(["id"], "gp"),
        run_post_create_sqls=run_post_create_sqls,
    )

    assert plan.populate_sql is None
    assert len(commands) == (3 if run_post_create_sqls else 1)


def test_cluster_routed_snapshot_append_uses_routed_insert_without_column_target() -> None:
    internal = resolve_internal_columns(["id"], "ch")
    append_sql = source_snapshot.build_append_snapshot_slice_sql(
        backend="ch",
        snapshot_table=f"stage.{SOURCE_STAGE}",
        source_columns=["id"],
        internal_columns=internal,
        snapshot_select_sql="SELECT id, 1, 1 FROM analytics.source",
        cluster_routed=True,
    )

    routed = route_sql(
        append_sql,
        routing=ChClusterRouting("core", "rand()"),
        database="default",
    )

    assert "INSERT INTO FUNCTION cluster('core', 'stage'," in routed
    assert "FROM cluster('core', 'analytics', 'source')" in routed


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


def test_cluster_routed_source_uses_ready_merge_tree_stage_on_routing_cluster(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(_connections(wait_policy="wait_none"))

    options = transfer_api.build_transfer_options(
        from_db="routed",
        to_db="gp",
        from_sql="SELECT 1 AS id",
        to_table="analytics.target",
    )

    assert options.source_staging_ch_policy.shard_engine == "MergeTree"
    assert options.source_staging_ch_policy.shard_on_cluster == "core"
    assert options.source_staging_ch_policy.ddl_wait_policy == "wait_shard"


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


def test_cluster_routed_source_rejects_replicated_stage_before_connecting(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(_connections("ReplicatedMergeTree('/stage/{table}', '{replica}')"))

    with pytest.raises(SqlConfigError, match="requires transfer staging engine MergeTree"):
        transfer_api.build_transfer_options(
            from_db="routed",
            to_db="gp",
            from_sql="SELECT 1 AS id",
            to_table="analytics.target",
        )


def test_cluster_routed_source_stage_validation_is_bypassed_when_staging_is_ignored(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(_connections("ReplicatedMergeTree('/stage/{table}', '{replica}')"))

    options = transfer_api.build_transfer_options(
        from_db="routed",
        to_db="gp",
        from_sql="SELECT 1 AS id",
        to_table="analytics.target",
        ignore_source_staging=True,
    )

    assert options.source_transfer_staging_schema is None
    assert options.source_staging_ch_policy is None


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


def test_cluster_routed_source_rejects_distributed_stage_pair(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(_connections(staging_pair=True))

    with pytest.raises(SqlConfigError, match="create_distributed_pair=false"):
        transfer_api.build_transfer_options(
            from_db="routed",
            to_db="gp",
            from_sql="SELECT 1 AS id",
            to_table="analytics.target",
        )
