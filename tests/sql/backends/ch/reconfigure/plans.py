from __future__ import annotations

from tests.sql._support.reconfigure import (
    LocalReconfigureClient,
    ReconfigureClient,
    SimpleNamespace,
    SqlOperationResult,
    SqlPlan,
    _options,
    get_backend_adapter,
    plan_ch_table_reconfiguration,
    pytest,
    reconfigure_api,
    sql,
)


def test_already_satisfied_change_returns_no_op_plan() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        ReconfigureClient(),
        _options(
            ch_engine="ReplicatedMergeTree('/clickhouse/{table}', '{replica}')",
        ),
    )

    assert reconfiguration.strategy == "no_op"
    assert reconfiguration.plan.sqls == []


def test_cross_cluster_plan_routes_wrapper_and_drops_source_shard() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        ReconfigureClient(),
        _options(
            ch_shard_on_cluster="archive",
            ch_distributed_cluster="archive",
            ch_distributed_on_cluster="{cluster}",
            ch_engine="MergeTree",
        ),
    )

    assert reconfiguration.strategy == "cross_cluster_rebuild"
    rendered = "\n".join(reconfiguration.plan.sqls)
    assert "ON CLUSTER 'archive'" in rendered
    assert "Distributed(" in rendered
    assert "'archive'" in rendered
    assert "'events_shard'" in rendered
    assert "DROP TABLE IF EXISTS analytics.events_shard ON CLUSTER '{cluster}'" in rendered


def test_ordinary_database_plan_uses_rename_fallback() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        ReconfigureClient(database_engine="Ordinary"),
        _options(ch_engine="MergeTree"),
    )

    rendered = "\n".join(reconfiguration.plan.sqls)
    assert "RENAME TABLE analytics.events_shard TO analytics.events_shard__backup_" in rendered
    assert "EXCHANGE TABLES" not in rendered
    assert len(reconfiguration.rollback_sqls) == 2


def test_public_dry_run_and_settings_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    clients: list[ReconfigureClient] = []

    def open_connection(_db_key: str) -> ReconfigureClient:
        client = ReconfigureClient()
        clients.append(client)
        return client

    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _db_key: SimpleNamespace(connection_key="ch", backend="ch"),
    )
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", open_connection)

    plan = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_settings={"index_granularity": 4096},
        dry_run=True,
    )
    return_sql_plan = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_distributed_cluster="core",
        ch_distributed_on_cluster="{cluster}",
        ch_sharding_key="cityHash64(id)",
        validate_row_count=False,
        retry_cnt=1,
        timeout_increment=0,
        query_label="test=reconfigure",
        return_sql=True,
    )
    result = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_settings={"index_granularity": 4096},
        return_metadata=True,
    )

    assert isinstance(plan, SqlPlan)
    assert isinstance(return_sql_plan, SqlPlan)
    assert isinstance(result, SqlOperationResult)
    assert result.data["strategy"] == "settings"
    assert clients[0].commands == []
    assert clients[1].commands == []
    assert clients[2].commands == [
        "ALTER TABLE analytics.events_shard ON CLUSTER '{cluster}' "
        "MODIFY SETTING index_granularity=4096"
    ]
    assert all(client.closed for client in clients)


def test_standalone_table_builds_local_rebuild_plan() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        LocalReconfigureClient(),
        _options(table="analytics.local_events", order_by="id"),
    )

    assert reconfiguration.strategy == "local_rebuild"
    assert reconfiguration.source_cluster is None
    assert "ON CLUSTER" not in "\n".join(reconfiguration.plan.sqls)


def test_structural_change_builds_atomic_managed_pair_plan() -> None:
    client = ReconfigureClient()

    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        client,
        _options(
            ch_engine="MergeTree",
            partition_by="toMonday(dt)",
            order_by=["id", "dt"],
        ),
    )

    assert reconfiguration.strategy == "managed_pair_rebuild"
    rendered = "\n".join(reconfiguration.plan.sqls)
    assert "ENGINE=MergeTree" in rendered
    assert any(
        partition_sql in rendered
        for partition_sql in (
            "PARTITION BY toMonday(dt)",
            "PARTITION BY dateTrunc('WEEK', dt)",
        )
    )
    assert 'ORDER BY ("id", "dt")' in rendered
    assert "INSERT INTO analytics.events__copy_" in rendered
    assert "EXCHANGE TABLES analytics.events_shard AND" in rendered
    assert "INDEX idx_id" in rendered
