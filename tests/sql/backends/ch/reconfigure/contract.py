from __future__ import annotations

from tests.sql._support.reconfigure import (
    SHARD_DDL,
    TABLE_DDL,
    ChReconfigureOptions,
    CountingReconfigureClient,
    FakeClickHouseResult,
    InvalidSqlInputError,
    LocalReconfigureClient,
    ReconfigureClient,
    SimpleNamespace,
    _options,
    _regular_defaults,
    exp,
    get_backend_adapter,
    plan_ch_table_reconfiguration,
    pytest,
    reconfigure_api,
    reconfigure_backend,
    reconfigure_ddl,
    reconfigure_execution,
    reconfigure_support,
    sql,
)


def test_backend_metadata_helpers_cover_empty_and_unqualified_cases() -> None:
    empty = SimpleNamespace(query=lambda _sql: FakeClickHouseResult([]))
    assert reconfigure_backend._count_rows(empty, "analytics.events") == 0
    assert reconfigure_backend._resolve_optional_cluster(empty, None) is None
    assert reconfigure_backend._table_exists_on_cluster(empty, "analytics.events", None) is False
    with pytest.raises(InvalidSqlInputError, match="does not exist"):
        reconfigure_backend._show_create_table(empty, "analytics.missing")
    with pytest.raises(InvalidSqlInputError, match="returned no rows"):
        reconfigure_backend._query_scalar(empty, "SELECT nothing")
    with pytest.raises(InvalidSqlInputError, match="must not be empty"):
        reconfigure_backend._non_empty_string(" ", "value")

    create = reconfigure_ddl.parse_create_table(
        "CREATE TABLE local_events (id UInt8) ENGINE=MergeTree ORDER BY id",
        "local_events",
    )
    current = SimpleNamespace(query=lambda _sql: FakeClickHouseResult([("analytics",)]))
    assert reconfigure_backend._table_database(current, create) == "analytics"
    assert reconfigure_backend._qualify_like("local_events", "shard") == "shard"
    assert reconfigure_backend._qualify_with_database("local_events", "analytics") == (
        "analytics.local_events"
    )
    assert reconfigure_backend._qualify_with_database("analytics.local_events", "other") == (
        "analytics.local_events"
    )


def test_cluster_count_uses_current_database_for_unqualified_table() -> None:
    queries: list[str] = []

    def query(sql: str) -> FakeClickHouseResult:
        queries.append(sql)
        return FakeClickHouseResult([(3,)])

    rows = reconfigure_support.count_rows_on_cluster(
        SimpleNamespace(query=query),
        "events_shard",
        "core",
        query_label="fresh target",
    )

    assert rows == 3
    assert queries == [
        "/* analytics_toolkit query_label=fresh target */\n"
        "SELECT count(*) FROM cluster('core', currentDatabase(), 'events_shard')"
    ]


def test_conversion_and_cluster_relocation_are_rejected() -> None:
    with pytest.raises(InvalidSqlInputError, match="separate"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            ReconfigureClient(),
            _options(
                ch_distributed_table=False,
                ch_shard_on_cluster="archive",
                ch_distributed_on_cluster="{cluster}",
            ),
        )


def test_cross_cluster_refuses_existing_destination_shard() -> None:
    class ExistingDestinationClient(ReconfigureClient):
        def query(self, query: str) -> FakeClickHouseResult:
            if "clusterAllReplicas('archive', system, tables)" in query:
                self.queries.append(query)
                return FakeClickHouseResult([(1,)])
            return super().query(query)

    with pytest.raises(InvalidSqlInputError, match="already contains"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            ExistingDestinationClient(),
            _options(
                ch_shard_on_cluster="archive",
                ch_distributed_cluster="archive",
                ch_distributed_on_cluster="{cluster}",
                ch_engine="MergeTree",
            ),
        )


def test_local_to_pair_conversion_uses_regular_defaults() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        LocalReconfigureClient(),
        _options(
            table="analytics.local_events",
            ch_distributed_table=True,
            regular_defaults=_regular_defaults(),
        ),
    )

    rendered = "\n".join(reconfiguration.plan.sqls)
    assert reconfiguration.strategy == "local_to_pair"
    assert "analytics.local_events_shard" in rendered
    assert "Distributed(" in rendered
    assert reconfiguration.source_pair is False
    assert reconfiguration.target_pair is True


@pytest.mark.parametrize(
    ("table_ddl", "message"),
    [
        (
            TABLE_DDL.replace("'events_shard'", "'external_events'"),
            "managed Distributed/_shard pair",
        ),
        (
            TABLE_DDL.replace(
                "Distributed('{cluster}', 'analytics', 'events_shard', rand())",
                "Distributed()",
            ),
            "unsupported Distributed engine",
        ),
    ],
)
def test_managed_pair_shape_is_validated(
    monkeypatch: pytest.MonkeyPatch,
    table_ddl: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        reconfigure_backend,
        "_show_create_table",
        lambda _connection, _table: table_ddl,
    )

    with pytest.raises(InvalidSqlInputError, match=message):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            ReconfigureClient(),
            _options(ch_settings={"index_granularity": 4096}),
        )


@pytest.mark.parametrize(
    ("client", "options", "expected_backup"),
    [
        (
            ReconfigureClient(database_engine="Ordinary"),
            _options(ch_engine="MergeTree"),
            "analytics.events_shard__backup_",
        ),
        (
            LocalReconfigureClient(database_engine="Ordinary"),
            _options(table="analytics.local_events", ch_engine="AggregatingMergeTree"),
            "analytics.local_events__backup_",
        ),
        (
            ReconfigureClient(database_engine="Ordinary"),
            _options(
                ch_engine="MergeTree",
                ch_sharding_key="cityHash64(id)",
                ch_distributed_on_cluster="{cluster}",
            ),
            "analytics.events__backup_",
        ),
        (
            ReconfigureClient(database_engine="Ordinary"),
            _options(
                ch_engine="MergeTree",
                ch_shard_on_cluster="archive",
                ch_distributed_cluster="archive",
                ch_distributed_on_cluster="{cluster}",
            ),
            "analytics.events__backup_",
        ),
    ],
)
def test_non_atomic_rebuilds_track_rename_backups(
    client: ReconfigureClient,
    options: ChReconfigureOptions,
    expected_backup: str,
) -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        client,
        options,
    )

    assert any(name.startswith(expected_backup) for name in reconfiguration.backup_tables)


def test_noop_execution_finishes_without_commands() -> None:
    client = ReconfigureClient()
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="ReplicatedMergeTree('/clickhouse/{table}', '{replica}')"),
    )

    reconfigure_backend.execute_ch_table_reconfiguration(
        adapter,
        client,
        reconfiguration,
        validate_row_count=True,
    )

    assert reconfiguration.cleanup_complete is True
    assert client.commands == []


def test_partially_overlapping_clusters_are_rejected() -> None:
    shared = ("shared", "10.0.0.1", 9000)
    client = ReconfigureClient(
        source_hosts=(shared, ("source", "10.0.0.2", 9000)),
        target_hosts=(shared, ("target", "10.0.0.3", 9000)),
    )

    with pytest.raises(InvalidSqlInputError, match="Partially overlapping"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            client,
            _options(
                ch_shard_on_cluster="archive",
                ch_distributed_cluster="archive",
                ch_distributed_on_cluster="{cluster}",
                ch_engine="MergeTree",
            ),
        )


def test_public_option_conflicts_fail_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _db_key: SimpleNamespace(connection_key="ch", backend="ch"),
    )
    monkeypatch.setattr(
        reconfigure_api,
        "get_sql_connection",
        lambda _db_key: pytest.fail("validation should not open a connection"),
    )

    with pytest.raises(InvalidSqlInputError, match="cannot be combined"):
        sql.ch_reconfigure_table(
            "ch",
            "analytics.events",
            partition_by="dt",
            reset_partition_by=True,
        )


def test_rebuild_aborts_before_cutover_when_source_count_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CountingReconfigureClient([3, 3, 4])
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="MergeTree"),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)
    monkeypatch.setattr(reconfigure_backend, "_wait_for_cleanup", lambda *_: None)

    with pytest.raises(RuntimeError, match="source row count changed"):
        reconfigure_backend.execute_ch_table_reconfiguration(
            adapter,
            client,
            reconfiguration,
            validate_row_count=True,
        )

    assert not any(command.startswith("EXCHANGE TABLES ") for command in client.commands)


def test_rebuild_detects_replacement_count_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CountingReconfigureClient([3, 2, 3])
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="MergeTree"),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)

    with pytest.raises(RuntimeError, match="replacement row count"):
        reconfigure_backend.execute_ch_table_reconfiguration(
            adapter,
            client,
            reconfiguration,
            validate_row_count=True,
        )


def test_reconfigure_ddl_replicated_path_and_property_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = reconfigure_ddl.parse_create_table(
        "CREATE TABLE x (id UInt8) ENGINE=MergeTree ORDER BY id",
        "x",
    )
    transformed = reconfigure_ddl.retarget_create(create, "y", "core")
    assert "ON CLUSTER 'core'" in transformed.sql(dialect="clickhouse")
    with pytest.raises(InvalidSqlInputError, match="must contain"):
        reconfigure_ddl.transform_create_table(
            create,
            table_name="x",
            execution_cluster=None,
            ch_engine="ReplicatedMergeTree('/fixed/path', '{replica}')",
            ch_partition_by=None,
            ch_order_by=None,
            ch_settings=None,
            ch_reset_partition_by=False,
            ch_reset_order_by=False,
        )
    reconfigure_ddl._validate_replicated_path(create)

    distributed = reconfigure_ddl.parse_create_table(
        "CREATE TABLE x (id UInt8) ENGINE=Distributed('core', 'db', 'shard')",
        "x",
    )
    assert reconfigure_ddl._distributed_sharding_key(distributed) is None
    assert reconfigure_ddl._distributed_sharding_key(create) is None
    city_hash = reconfigure_ddl.parse_create_table(
        "CREATE TABLE x (id UInt8) ENGINE=Distributed('core', 'db', 'shard', cityHash64(id))",
        "x",
    )
    assert reconfigure_ddl._distributed_sharding_key(city_hash) == "cityHash64(id)"
    rand_key = reconfigure_ddl.parse_create_table(
        "CREATE TABLE x (id UInt8) ENGINE=Distributed('core', 'db', 'shard', rand())",
        "x",
    )
    assert reconfigure_ddl._distributed_sharding_key(rand_key) == "rand()"
    assert reconfigure_ddl.distributed_table_parts("events") == ("default", "events")

    with pytest.raises(InvalidSqlInputError, match="template is required"):
        reconfigure_ddl.transform_distributed_create(
            create,
            table_name="facade",
            execution_cluster=None,
            target_cluster="core",
            shard_table="analytics.events_shard",
            ch_sharding_key=None,
        )
    three_argument_distributed = reconfigure_ddl.transform_distributed_create(
        distributed,
        table_name="facade",
        execution_cluster=None,
        target_cluster="core",
        shard_table="analytics.events_shard",
        ch_sharding_key="cityHash64(id)",
    )
    assert "cityHash64(id)" in three_argument_distributed.sql(dialect="clickhouse")
    templated = reconfigure_ddl.transform_distributed_create(
        create,
        table_name="facade",
        execution_cluster="core",
        target_cluster="core",
        shard_table="analytics.events_shard",
        ch_sharding_key=None,
        ch_distributed_engine_template=(
            "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
        ),
    )
    assert "rand()" in templated.sql(dialect="clickhouse").lower()
    templated_without_key = reconfigure_ddl.transform_distributed_create(
        create,
        table_name="facade",
        execution_cluster=None,
        target_cluster="core",
        shard_table="analytics.events_shard",
        ch_sharding_key=None,
        ch_distributed_engine_template="Distributed({cluster}, {database}, {shard_table})",
    )
    assert "rand()" in templated_without_key.sql(dialect="clickhouse").lower()

    monkeypatch.setattr(reconfigure_ddl, "validate_distributed_template", lambda _template: None)
    with pytest.raises(InvalidSqlInputError, match="at least three arguments"):
        reconfigure_ddl._distributed_args_from_template(
            "Distributed('core')",
            target_cluster="core",
            database="analytics",
            shard_table="events_shard",
            sharding_key=None,
        )

    settings_free = reconfigure_ddl.parse_create_table(
        "CREATE TABLE x (id UInt8) ENGINE=MergeTree ORDER BY id",
        "x",
    )
    reconfigure_ddl._apply_settings_to_create(settings_free, {"index_granularity": 4096})
    assert "index_granularity = 4096" in settings_free.sql(dialect="clickhouse")
    unusual_settings = exp.Create(
        this=exp.Schema(this=exp.to_table("x")),
        kind="TABLE",
        properties=exp.Properties(
            expressions=[exp.SettingsProperty(expressions=[exp.Literal.number(1)])]
        ),
    )
    reconfigure_ddl._apply_settings_to_create(unusual_settings, {"new_setting": 1})

    replicated_without_args = exp.Create(
        this=exp.Schema(this=exp.to_table("x")),
        kind="TABLE",
        properties=exp.Properties(
            expressions=[exp.EngineProperty(this=exp.Anonymous(this="ReplicatedMergeTree"))]
        ),
    )
    reconfigure_ddl._validate_replicated_path(replicated_without_args)
    replicated_dynamic_path = replicated_without_args.copy()
    engine_property = reconfigure_ddl._property(
        replicated_dynamic_path,
        exp.EngineProperty,
    )
    assert isinstance(engine_property, exp.EngineProperty)
    assert isinstance(engine_property.this, exp.Anonymous)
    engine_property.this.append("expressions", exp.column("path_column"))
    reconfigure_ddl._validate_replicated_path(replicated_dynamic_path)

    transient_type = type("UuidProperty", (exp.Expression,), {})
    assert reconfigure_ddl._is_transient_create_property(transient_type()) is True


def test_remote_shard_metadata_reraises_when_cluster_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableShardClient(ReconfigureClient):
        def query(self, query: str) -> FakeClickHouseResult:
            if query == "SHOW CREATE TABLE analytics.events_shard":
                message = "shard is not local"
                raise RuntimeError(message)
            return super().query(query)

    monkeypatch.setattr(reconfigure_backend, "_resolve_optional_cluster", lambda *_args: None)
    with pytest.raises(RuntimeError, match="shard is not local"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            UnavailableShardClient(),
            _options(ch_settings={"index_granularity": 4096}),
        )


def test_remote_shard_metadata_supports_separate_facade_and_shard_clusters() -> None:
    class RemoteShardClient(ReconfigureClient):
        def query(self, query: str) -> FakeClickHouseResult:
            if query == "SHOW CREATE TABLE analytics.events_shard":
                message = "shard is not local"
                raise RuntimeError(message)
            if "SELECT create_table_query FROM clusterAllReplicas" in query:
                self.queries.append(query)
                return FakeClickHouseResult([(SHARD_DDL,), (SHARD_DDL,)])
            return super().query(query)

    client = RemoteShardClient()
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        client,
        _options(ch_settings={"index_granularity": 4096}),
    )

    assert reconfiguration.strategy == "settings"
    assert any("create_table_query FROM clusterAllReplicas" in query for query in client.queries)
    assert "ON CLUSTER '{cluster}'" in reconfiguration.plan.sqls[0]


def test_reset_flags_remove_partition_and_restore_tuple_order() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        ReconfigureClient(),
        _options(reset_partition_by=True, reset_order_by=True),
    )

    desired = reconfiguration.after_ddl["shard"]
    assert "PARTITION BY" not in desired
    assert "ORDER BY tuple()" in desired


def test_reset_only_setting_sql_and_public_none_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert reconfigure_backend._build_setting_alter_sqls(
        "analytics.events",
        {"old_setting": None},
        ch_cluster=None,
    ) == ["ALTER TABLE analytics.events RESET SETTING old_setting"]
    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _db_key: SimpleNamespace(connection_key="ch", backend="ch"),
    )
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _db_key: ReconfigureClient())

    result = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_settings={"index_granularity": 4096},
        retry_cnt=1,
    )

    assert result is None


def test_settings_change_uses_direct_alter_and_preserves_ddl() -> None:
    client = ReconfigureClient()

    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        client,
        _options(ch_settings={"index_granularity": 4096, "old_setting": None}),
    )

    assert reconfiguration.strategy == "settings"
    assert reconfiguration.plan.sqls == [
        "ALTER TABLE analytics.events_shard ON CLUSTER '{cluster}' "
        "MODIFY SETTING index_granularity=4096",
        "ALTER TABLE analytics.events_shard ON CLUSTER '{cluster}' RESET SETTING old_setting",
    ]
    assert "INDEX idx_id" in reconfiguration.after_ddl["shard"]
    assert "index_granularity = 4096" in reconfiguration.after_ddl["shard"]


def test_synchronous_execution_falls_back_for_simple_clients() -> None:
    class LegacyClient(ReconfigureClient):
        def command(self, query: str, settings: object = None) -> None:
            if settings is not None:
                message = "settings unsupported"
                raise TypeError(message)
            super().command(query)

    client = LegacyClient()
    reconfigure_execution.execute_reconfiguration_sqls(
        get_backend_adapter("ch"),
        client,
        [
            "ALTER TABLE analytics.events ON CLUSTER core MODIFY COMMENT 'x'",
            "OPTIMIZE TABLE x",
        ],
    )

    assert len(client.commands) == 2
    assert reconfigure_execution.cluster_clause(None) == ""


def test_to_defaults_converges_policy_then_applies_explicit_overrides() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        ReconfigureClient(),
        _options(
            to_defaults=True,
            regular_defaults=_regular_defaults(),
            ch_sharding_key="cityHash64(dt)",
        ),
    )

    rendered = "\n".join(reconfiguration.plan.sqls)
    assert reconfiguration.strategy == "managed_pair_rebuild"
    assert "ENGINE=MergeTree" in rendered
    assert "cityHash64(dt)" in rendered
    assert reconfiguration.distributed_on_cluster == "{cluster}"
    assert reconfiguration.distributed_cluster == "core"


def test_wrapper_only_change_uses_wrapper_recreate_strategy() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        ReconfigureClient(),
        _options(
            ch_sharding_key="cityHash64(id)",
            ch_distributed_on_cluster="{cluster}",
        ),
    )

    assert reconfiguration.strategy == "wrapper_recreate"
    assert reconfiguration.replacement_table is not None
