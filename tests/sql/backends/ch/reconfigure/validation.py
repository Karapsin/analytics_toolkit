from __future__ import annotations

from tests.sql._support.reconfigure import (
    SHARD_DDL,
    CountingReconfigureClient,
    FakeClickHouseResult,
    InvalidSqlInputError,
    LocalReconfigureClient,
    ReconfigureClient,
    SimpleNamespace,
    SqlConfigError,
    UnsupportedConnectionTypeError,
    _options,
    exp,
    get_backend_adapter,
    plan_ch_table_reconfiguration,
    pytest,
    reconfigure_api,
    reconfigure_backend,
    reconfigure_ddl,
    reconfigure_policy,
    reconfigure_support,
    sql,
)


def test_cluster_comparison_validates_empty_and_equal_hosts() -> None:
    client = ReconfigureClient(source_hosts=(), target_hosts=())
    with pytest.raises(InvalidSqlInputError, match="source cluster"):
        reconfigure_backend._is_cross_cluster(client, "core", "archive")
    client = ReconfigureClient(target_hosts=())
    with pytest.raises(InvalidSqlInputError, match="target cluster"):
        reconfigure_backend._is_cross_cluster(client, "core", "archive")
    same = (("same", "10.0.0.1", 9000),)
    client = ReconfigureClient(source_hosts=same, target_hosts=same)
    assert reconfigure_backend._is_cross_cluster(client, "core", "archive") is False
    assert reconfigure_backend._is_cross_cluster(client, "core", "core") is False


def test_cluster_count_and_missing_routing_helpers() -> None:
    populated = SimpleNamespace(query=lambda _sql: FakeClickHouseResult([(7,)]))
    empty = SimpleNamespace(query=lambda _sql: FakeClickHouseResult([]))
    assert reconfigure_support.count_final_rows(populated, "analytics.events", "core") == 7
    assert reconfigure_support.count_rows_on_cluster(empty, "analytics.events", "core") == 0

    with pytest.raises(InvalidSqlInputError, match="requires ch_distributed_cluster"):
        reconfigure_policy.resolve_desired_reconfigure_policy(
            _options(),
            source_pair=True,
            source_shard_engine="MergeTree",
            source_shard_cluster=None,
            source_distributed_cluster=None,
        )


def test_cross_cluster_requires_managed_pair() -> None:
    with pytest.raises(InvalidSqlInputError, match="requires a managed"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            LocalReconfigureClient(),
            _options(
                table="analytics.local_events",
                ch_shard_on_cluster="archive",
                ch_engine="MergeTree",
            ),
        )


def test_existing_facade_change_requires_explicit_management_scope() -> None:
    with pytest.raises(InvalidSqlInputError, match="ch_distributed_on_cluster is required"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            ReconfigureClient(),
            _options(
                ch_distributed_cluster="archive",
                ch_settings={"index_granularity": 4096},
            ),
        )


def test_pair_to_local_conversion_requires_and_uses_facade_scope() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        ReconfigureClient(),
        _options(
            ch_distributed_table=False,
            ch_shard_on_cluster="{cluster}",
            ch_distributed_on_cluster="{cluster}",
        ),
    )

    assert reconfiguration.strategy == "pair_to_local"
    assert reconfiguration.source_pair is True
    assert reconfiguration.target_pair is False
    assert any("RENAME TABLE analytics.events TO" in sql for sql in reconfiguration.cutover_sqls)


def test_public_rejects_non_clickhouse_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _db_key: SimpleNamespace(connection_key="trino", backend="trino"),
    )
    with pytest.raises(UnsupportedConnectionTypeError):
        sql.ch_reconfigure_table(
            "trino",
            "analytics.events",
            ch_settings={"index_granularity": 4096},
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"order_by": "id", "reset_order_by": True},
        {"ch_settings": ["not", "a", "mapping"]},
        {"ch_distributed_table": "yes"},
        {"validate_row_count": "yes"},
    ],
)
def test_public_validation_rejects_invalid_option_types(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
) -> None:
    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _db_key: SimpleNamespace(connection_key="ch", backend="ch"),
    )
    with pytest.raises(InvalidSqlInputError):
        sql.ch_reconfigure_table("ch", "analytics.events", **kwargs)


def test_rebuild_execution_validates_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CountingReconfigureClient([3, 3, 3, 3])
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="MergeTree"),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)
    monkeypatch.setattr(reconfigure_backend, "_wait_for_cleanup", lambda *_: None)

    reconfigure_backend.execute_ch_table_reconfiguration(
        adapter,
        client,
        reconfiguration,
        validate_row_count=True,
    )

    assert reconfiguration.cleanup_complete is True
    assert reconfiguration.plan.metadata.row_count_validated is True
    assert any(command.startswith("INSERT INTO ") for command in client.commands)
    assert sum(command.startswith("EXCHANGE TABLES ") for command in client.commands) == 1
    assert any(command.startswith("DROP TABLE IF EXISTS ") for command in client.commands)
    assert all(
        settings
        == {
            "distributed_ddl_task_timeout": 300,
            "distributed_ddl_output_mode": "throw_only_active",
        }
        for command, settings in zip(client.commands, client.command_settings)
        if "ON CLUSTER" in command
    )


def test_rebuild_rolls_back_failed_post_cutover_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CountingReconfigureClient([3, 3, 3, 2])
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="MergeTree"),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)
    monkeypatch.setattr(reconfigure_backend, "_wait_for_cleanup", lambda *_: None)

    with pytest.raises(RuntimeError, match="during cutover"):
        reconfigure_backend.execute_ch_table_reconfiguration(
            adapter,
            client,
            reconfiguration,
            validate_row_count=True,
        )

    assert sum(command.startswith("EXCHANGE TABLES ") for command in client.commands) == 2


def test_reconfiguration_requires_at_least_one_change() -> None:
    with pytest.raises(InvalidSqlInputError, match="At least one"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            ReconfigureClient(),
            _options(),
        )


def test_reconfigure_ddl_validation_and_value_rendering() -> None:
    with pytest.raises(InvalidSqlInputError, match="Could not parse"):
        reconfigure_ddl.parse_create_table("CREATE TABLE", "broken")
    with pytest.raises(InvalidSqlInputError, match="not a supported"):
        reconfigure_ddl.parse_create_table("SELECT 1", "query")
    with pytest.raises(InvalidSqlInputError, match="no explicit schema"):
        reconfigure_ddl.parse_create_table("CREATE TABLE x AS SELECT 1", "x")

    no_properties = exp.Create(this=exp.Schema(this=exp.to_table("x")), kind="TABLE")
    with pytest.raises(InvalidSqlInputError, match="does not define an engine"):
        reconfigure_ddl.engine_sql(no_properties)
    with pytest.raises(InvalidSqlInputError, match="does not define an engine"):
        reconfigure_ddl.engine_name(no_properties)
    with pytest.raises(InvalidSqlInputError, match="MergeTree-family"):
        reconfigure_ddl.require_merge_tree(
            reconfigure_ddl.parse_create_table(
                "CREATE TABLE x (id UInt8) ENGINE=Log",
                "x",
            ),
            "x",
        )
    with pytest.raises(InvalidSqlInputError, match="no column schema"):
        reconfigure_ddl.retarget_create(
            exp.Create(this=exp.to_table("x"), kind="TABLE"),
            "y",
            None,
        )

    with pytest.raises(InvalidSqlInputError, match="Invalid expression"):
        reconfigure_ddl.expression_sql("(", "expression")
    for invalid in ({"id": 1}, b"id"):
        with pytest.raises(InvalidSqlInputError, match="SQL expression"):
            reconfigure_ddl.expression_sql(invalid, "expression")
    with pytest.raises(InvalidSqlInputError, match="must not be empty"):
        reconfigure_ddl.expression_sql([], "expression")
    with pytest.raises(InvalidSqlInputError, match="duplicates"):
        reconfigure_ddl.expression_sql(["id", "id"], "expression")
    assert reconfigure_ddl.expression_sql(["id"], "expression") == "`id`"

    assert reconfigure_ddl.setting_value_sql(True) == "1"
    assert reconfigure_ddl.setting_value_sql(3) == "3"
    assert reconfigure_ddl.setting_value_sql(1.5) == "1.5"
    assert reconfigure_ddl.setting_value_sql("value") == "'value'"
    with pytest.raises(InvalidSqlInputError, match="finite"):
        reconfigure_ddl.setting_value_sql(float("inf"))
    with pytest.raises(InvalidSqlInputError, match="strings"):
        reconfigure_ddl.setting_value_sql(object())
    with pytest.raises(InvalidSqlInputError, match="setting name"):
        reconfigure_ddl.normalize_setting_name("bad-name")
    with pytest.raises(InvalidSqlInputError, match="must not be empty"):
        reconfigure_ddl._non_empty_string("", "value")


def test_remote_shard_metadata_rejects_missing_and_inconsistent_ddl() -> None:
    empty = SimpleNamespace(query=lambda _sql: FakeClickHouseResult([]))
    with pytest.raises(InvalidSqlInputError, match="does not exist on cluster"):
        reconfigure_support.show_create_table_on_cluster(
            empty,
            "analytics.events_shard",
            "core",
        )

    inconsistent = SimpleNamespace(
        query=lambda _sql: FakeClickHouseResult(
            [(SHARD_DDL,), (SHARD_DDL.replace("ORDER BY (dt, id)", "ORDER BY id"),)]
        )
    )
    with pytest.raises(InvalidSqlInputError, match="inconsistent DDL"):
        reconfigure_support.show_create_table_on_cluster(
            inconsistent,
            "analytics.events_shard",
            "core",
        )


def test_to_defaults_requires_regular_clickhouse_defaults() -> None:
    with pytest.raises(SqlConfigError, match="regular ddl_defaults"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            ReconfigureClient(),
            _options(to_defaults=True),
        )
