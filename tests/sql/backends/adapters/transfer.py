from __future__ import annotations

from analytics_toolkit.sql.backends.models import StageFinalizationRequest

from tests.sql._support.adapters import (
    FakeDbapiConnection,
    RecordingClickHouseClient,
    SimpleNamespace,
    get_backend_adapter,
    pd,
    pytest,
    table_ops_module,
)


def test_backend_transfer_and_load_policies_are_adapter_owned() -> None:
    gp_adapter = get_backend_adapter("gp")
    trino_adapter = get_backend_adapter("trino")
    ch_adapter = get_backend_adapter("ch")

    assert gp_adapter.target_connection_defaults(SimpleNamespace()).insert_chunk_size is None
    trino_defaults = trino_adapter.target_connection_defaults(
        SimpleNamespace(
            insert_chunk_size=123,
            s3_transfer_staging_location="s3://bucket/stage",
            upsert_partition_drop_sql_template="DELETE FROM {table}",
        )
    )
    assert trino_defaults.insert_chunk_size == 123
    assert trino_defaults.s3_transfer_staging_location == "s3://bucket/stage"
    assert trino_defaults.upsert_partition_drop_sql_template == "DELETE FROM {table}"

    gp_policy = gp_adapter.transfer_attempt_policy(retry_cnt=5)
    assert gp_policy.insert_retry_cnt == 5
    assert gp_policy.retry_ambiguous_stage_load is False
    gp_sizing = gp_adapter.transfer_insert_page_sizing(gp_insert_chunk_size=None)
    assert gp_sizing is not None
    assert gp_sizing.initial_size == 10_000
    assert gp_sizing.min_size == 1_000
    assert gp_sizing.max_size == 100_000
    explicit_gp_sizing = gp_adapter.transfer_insert_page_sizing(
        gp_insert_chunk_size=50_000,
    )
    assert explicit_gp_sizing is not None
    assert explicit_gp_sizing.initial_size == 50_000
    assert explicit_gp_sizing.min_size == 1_000
    assert explicit_gp_sizing.max_size == 200_000

    for adapter in (trino_adapter, ch_adapter):
        policy = adapter.transfer_attempt_policy(retry_cnt=5)
        assert policy.insert_retry_cnt == 1
        assert policy.retry_ambiguous_stage_load is True
        assert adapter.transfer_insert_page_sizing(gp_insert_chunk_size=None) is None

    assert (
        trino_adapter.requires_load_target_column_metadata(
            write_mode="replace",
            original_target_exists=False,
        )
        is True
    )
    assert (
        gp_adapter.requires_load_target_column_metadata(
            write_mode="replace",
            original_target_exists=True,
        )
        is False
    )
    assert (
        gp_adapter.requires_load_target_column_metadata(
            write_mode="upsert",
            original_target_exists=True,
        )
        is True
    )
    assert gp_adapter.uses_partition_replacement_upsert() is False
    assert trino_adapter.uses_partition_replacement_upsert() is True
    assert ch_adapter.uses_partition_replacement_upsert() is True
    assert gp_adapter.needs_upsert_partition_drop_template() is False
    assert trino_adapter.needs_upsert_partition_drop_template() is True
    assert ch_adapter.needs_upsert_partition_drop_template() is False
    assert gp_adapter.supports_distributed_table_targets() is False
    assert trino_adapter.supports_distributed_table_targets() is False
    assert ch_adapter.supports_distributed_table_targets() is True
    assert gp_adapter.can_create_transfer_target_before_batches() is True
    assert trino_adapter.can_create_transfer_target_before_batches() is True
    assert ch_adapter.can_create_transfer_target_before_batches() is False
    assert gp_adapter.transfer_replace_existing_non_ch() == "drop"
    assert gp_adapter.allows_show_tables_catalog_filter() is False
    assert trino_adapter.allows_show_tables_catalog_filter() is True
    assert ch_adapter.allows_show_tables_catalog_filter() is False
    assert gp_adapter.validate_write_mode("append") == "append"
    assert trino_adapter.validate_write_mode("UPSERT") == "upsert"
    with pytest.raises(ValueError, match="must be one of"):
        ch_adapter.validate_write_mode("merge")
    original_gp_modes = gp_adapter.supported_write_modes
    gp_adapter.supported_write_modes = frozenset({"append"})
    try:
        with pytest.raises(ValueError, match="Greenplum does not support"):
            gp_adapter.validate_write_mode("upsert")
    finally:
        gp_adapter.supported_write_modes = original_gp_modes
    assert ch_adapter.normalize_ch_string(" id ", "order_by") == "id"
    assert ch_adapter.normalize_ch_columns_or_expression(
        [" id ", "dt"],
        "order_by",
    ) == ["id", "dt"]
    with pytest.raises(ValueError, match="duplicate column names"):
        ch_adapter.normalize_ch_columns_or_expression(["id", " id "], "order_by")
    ch_adapter.validate_ch_columns_in_columns(
        ["id"],
        ["id", "dt"],
        "order_by",
        data_name="staged data",
    )
    with pytest.raises(ValueError, match="missing"):
        ch_adapter.validate_ch_columns_in_columns(
            ["missing"],
            ["id"],
            "order_by",
            data_name="staged data",
        )
    assert gp_adapter.resolve_ch_retry_per_host_drops(True) is False
    assert trino_adapter.resolve_ch_retry_per_host_drops(True) is False
    assert ch_adapter.resolve_ch_retry_per_host_drops(True) is True
    assert ch_adapter.resolve_ch_retry_per_host_drops(False) is False
    create_batch = pd.DataFrame({"id": [1], "label": ["a"]})
    assert (
        gp_adapter.expected_create_table_column_types(
            create_batch,
            {"id": "BIGINT", "label": "TEXT"},
            ch_distributed_table=True,
            ch_only_shard=False,
        )
        is None
    )
    assert ch_adapter.expected_create_table_column_types(
        create_batch,
        {"id": "UInt64", "label": "String"},
        ch_distributed_table=True,
        ch_only_shard=False,
    ) == {"id": "UInt64", "label": "String"}
    assert (
        ch_adapter.expected_create_table_column_types(
            create_batch,
            {"id": "UInt64", "label": "String"},
            ch_distributed_table=False,
            ch_only_shard=False,
        )
        is None
    )
    assert (
        ch_adapter.expected_create_table_column_types(
            create_batch,
            {"id": "UInt64", "label": "String"},
            ch_distributed_table=True,
            ch_only_shard=True,
        )
        is None
    )

    gp_adapter.validate_gp_distributed_by_key_option(["id"], option_owner="to_db")
    gp_adapter.validate_gp_insert_chunk_size_option(1, option_owner="to_db")
    with pytest.raises(ValueError, match="positive integer"):
        gp_adapter.validate_gp_insert_chunk_size_option(0, option_owner="to_db")
    with pytest.raises(ValueError, match="to_db has type 'gp'"):
        trino_adapter.validate_gp_distributed_by_key_option(
            ["id"],
            option_owner="to_db",
        )
    with pytest.raises(ValueError, match="to_db has type 'gp'"):
        ch_adapter.validate_gp_insert_chunk_size_option(1000, option_owner="to_db")
    trino_adapter.validate_trino_insert_chunk_size_option(100, option_owner="to_db")
    with pytest.raises(ValueError, match="positive integer"):
        trino_adapter.validate_trino_insert_chunk_size_option(0, option_owner="to_db")
    gp_adapter.validate_trino_insert_chunk_size_option(1000, option_owner="to_db")
    ch_adapter.validate_trino_insert_chunk_size_option(1000, option_owner="to_db")
    with pytest.raises(ValueError, match="positive integer"):
        gp_adapter.validate_trino_insert_chunk_size_option(0, option_owner="to_db")
    with pytest.raises(ValueError, match="positive integer"):
        ch_adapter.validate_trino_insert_chunk_size_option(0, option_owner="to_db")
    ch_adapter.validate_ch_create_table_options(
        option_owner="to_db",
        partition_by=["dt"],
        order_by=["id"],
        ch_engine="ReplacingMergeTree",
        ch_cluster="cluster",
        ch_sharding_key="cityHash64(id)",
        ch_only_shard=True,
    )
    with pytest.raises(ValueError, match="ch_only_shard must be a boolean"):
        ch_adapter.validate_ch_create_table_options(
            option_owner="to_db",
            partition_by=None,
            order_by=None,
            ch_engine="ReplacingMergeTree",
            ch_cluster="cluster",
            ch_sharding_key="cityHash64(id)",
            ch_only_shard="yes",  # type: ignore[arg-type]
        )
    trino_adapter.validate_ch_create_table_options(
        option_owner="to_db",
        partition_by=["dt"],
        order_by=["id"],
        ch_engine="ReplicatedMergeTree",
        ch_cluster="{cluster}",
        ch_sharding_key="rand()",
        ch_only_shard=False,
    )
    with pytest.raises(ValueError, match="to_db has type 'ch'"):
        trino_adapter.validate_ch_create_table_options(
            option_owner="to_db",
            partition_by=None,
            order_by=None,
            ch_engine="ReplacingMergeTree",
            ch_cluster="{cluster}",
            ch_sharding_key="rand()",
            ch_only_shard=False,
        )
    with pytest.raises(ValueError, match="to_db has type 'gp'"):
        gp_adapter.validate_ch_create_table_options(
            option_owner="to_db",
            partition_by=None,
            order_by=["id"],
            ch_engine="ReplicatedMergeTree",
            ch_cluster="{cluster}",
            ch_sharding_key="rand()",
            ch_only_shard=False,
        )

    assert (
        trino_adapter.resolve_transfer_staging_mode(
            None,
            s3_transfer_staging_schema="tmp",
            s3_transfer_staging_location="s3://bucket/prefix",
        )
        == "parquet"
    )
    assert (
        trino_adapter.resolve_transfer_staging_mode(
            None,
            s3_transfer_staging_schema=None,
            s3_transfer_staging_location=None,
        )
        == "values"
    )
    with pytest.raises(ValueError, match="requires s3_transfer_staging_location"):
        trino_adapter.resolve_transfer_staging_mode(
            "parquet",
            s3_transfer_staging_schema="tmp",
            s3_transfer_staging_location=None,
        )
    with pytest.raises(ValueError, match="can only be used"):
        ch_adapter.resolve_transfer_staging_mode(
            "values",
            s3_transfer_staging_schema=None,
            s3_transfer_staging_location=None,
        )

    assert (
        gp_adapter.should_insert_create_table_from_sql_directly(
            source_backend="gp",
            source_key="source_gp",
            target_key="target_gp",
        )
        is True
    )
    assert (
        gp_adapter.should_insert_create_table_from_sql_directly(
            source_backend="trino",
            source_key="source_trino",
            target_key="target_gp",
        )
        is False
    )


def test_materialized_transfer_source_sql_is_backend_specific() -> None:
    assert (
        get_backend_adapter("gp").build_materialize_transfer_source_sql(
            "scratch.result",
            "SELECT * FROM source;",
        )
        == "CREATE TABLE scratch.result AS SELECT * FROM source DISTRIBUTED RANDOMLY"
    )
    assert (
        get_backend_adapter("trino").build_materialize_transfer_source_sql(
            "scratch.result",
            "SELECT * FROM source;",
        )
        == "CREATE TABLE scratch.result AS SELECT * FROM source"
    )
    assert get_backend_adapter("ch").build_materialize_transfer_source_sql(
        "scratch.result",
        "SELECT * FROM source;",
    ) == ("CREATE TABLE scratch.result ENGINE = MergeTree ORDER BY tuple() AS SELECT * FROM source")


def test_stage_base_identifier_policy_is_adapter_owned() -> None:
    assert get_backend_adapter("trino").stage_base_identifier("target", None, "abcd") == "target"
    assert get_backend_adapter("ch").stage_base_identifier("target", "loader", "abcd") == "target"


def test_target_create_kwargs_are_backend_adapter_owned() -> None:
    gp_adapter = get_backend_adapter("gp")
    ch_adapter = get_backend_adapter("ch")

    assert gp_adapter.should_ensure_load_target_table(target_exists=True) is False
    assert gp_adapter.build_load_target_create_kwargs(
        gp_distributed_by_key=["id"],
        partition_by="dt",
        order_by=None,
        ch_engine="ReplicatedMergeTree",
        ch_cluster="{cluster}",
        ch_sharding_key="rand()",
        ch_only_shard=False,
        write_mode="replace",
        original_target_exists=True,
    ) == {
        "gp_distributed_by_key": ["id"],
        "partition_by": "dt",
    }

    assert ch_adapter.should_ensure_load_target_table(target_exists=True) is True
    assert ch_adapter.build_load_target_create_kwargs(
        gp_distributed_by_key=None,
        partition_by="toYYYYMM(dt)",
        order_by=["id"],
        ch_engine="MergeTree",
        ch_cluster="cluster",
        ch_sharding_key="id",
        ch_only_shard=False,
        write_mode="replace",
        original_target_exists=True,
    ) == {
        "gp_distributed_by_key": None,
        "partition_by": "toYYYYMM(dt)",
        "order_by": ["id"],
        "ch_engine": "MergeTree",
        "ch_cluster": "cluster",
        "ch_sharding_key": "id",
        "ch_distributed_table": True,
        "ch_only_shard": False,
        "ch_replace_table": True,
    }
    assert (
        ch_adapter.build_create_from_sql_target_create_kwargs(
            gp_distributed_by_key=None,
            partition_by=None,
            order_by=None,
            ch_engine="MergeTree",
            ch_cluster="cluster",
            ch_sharding_key="id",
            ch_only_shard=True,
            drop_target_if_exists=True,
            target_exists_before_drop=True,
        )["ch_distributed_table"]
        is False
    )


def test_target_lifecycle_can_preserve_load_df_ch_truncate_missing_target() -> None:
    client = RecordingClickHouseClient()

    target_exists = table_ops_module.apply_target_write_mode(
        "ch",
        client,
        "db.target",
        write_mode="truncate_insert",
        target_exists=False,
        replace_existing_non_ch="drop",
        drop_missing_ch_truncate_target=False,
    )

    assert target_exists is False
    assert client.commands == []


def test_target_lifecycle_helper_preserves_non_ch_replace_modes() -> None:
    drop_connection = FakeDbapiConnection()
    target_exists = table_ops_module.apply_target_write_mode(
        "gp",
        drop_connection,
        "schema.target",
        write_mode="replace",
        target_exists=True,
        replace_existing_non_ch="drop",
    )
    assert target_exists is False
    assert drop_connection.executed == ["DROP TABLE IF EXISTS schema.target"]

    clear_connection = FakeDbapiConnection()
    target_exists = table_ops_module.apply_target_write_mode(
        "gp",
        clear_connection,
        "schema.target",
        write_mode="replace",
        target_exists=True,
        replace_existing_non_ch="clear",
    )
    assert target_exists is True
    assert clear_connection.executed == ["TRUNCATE TABLE schema.target"]


def test_gp_transfer_replace_recreates_target_from_staged_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("gp")
    staged_schema = {"dt": "DATE", "offer_code": "TEXT", "group_name": "TEXT"}
    events: list[object] = []
    monkeypatch.setattr(
        adapter,
        "drop_table",
        lambda *_args, **_kwargs: events.append("drop"),
    )
    monkeypatch.setattr(
        adapter,
        "ensure_stage_target_table",
        lambda request: events.append(("create", dict(request.target_column_types or {}))) or True,
    )
    monkeypatch.setattr(
        adapter,
        "insert_from_table",
        lambda *_args, **kwargs: events.append(("insert", dict(kwargs["column_types"] or {}))),
    )

    adapter.finalize_stage_table(
        StageFinalizationRequest(
            connection=object(),
            stage_table="stage.transfer_rows",
            target_table="sandbox.target",
            replace_target_table=True,
            target_exists=True,
            sample_batch=pd.DataFrame(columns=list(staged_schema)),
            target_column_types=staged_schema,
            insert_column_types=staged_schema,
            write_mode="replace",
        )
    )

    assert events == [
        "drop",
        ("create", staged_schema),
        ("insert", staged_schema),
    ]
