from __future__ import annotations

from tests.sql._support.load_table import (
    TEST_CH_SHARD_TABLE,
    TEST_CH_STAGE_TABLE,
    TEST_CH_TABLE,
    FakeClickHouseClient,
    SimpleNamespace,
    load_df_module,
    pd,
    table_ops_module,
)


def test_finalize_stage_table_clickhouse_upsert_drops_shard_partitions() -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame({"id": [1], "score": [10]})

    table_ops_module.finalize_stage_table(
        connection_type="ch",
        connection=client,
        stage_table=TEST_CH_STAGE_TABLE,
        target_table=TEST_CH_TABLE,
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        final_upsert_stage_table=f"{TEST_CH_TABLE}__final_stage",
        insert_column_types={"id": "UInt64", "score": "Int64"},
        ch_cluster="core",
    )

    drop_sql = next(sql for sql in client.commands if "DROP PARTITION" in sql)
    assert drop_sql == f"ALTER TABLE {TEST_CH_SHARD_TABLE} ON CLUSTER core DROP PARTITION 1"
    assert client.commands[-1].startswith(f"INSERT INTO {TEST_CH_TABLE} (`id`, `score`) ")


def test_load_df_drops_overlap_stage_table_after_success(monkeypatch) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    connection = FakeConnection()
    config = SimpleNamespace(
        connection_key="gp",
        backend="gp",
        user="target_user",
        transfer_staging_schema="transfer_schema",
        insert_chunk_size=None,
    )
    cleanups: list[tuple[str, str, str]] = []

    monkeypatch.setattr(load_df_module, "get_connection_config", lambda key: config)
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        load_df_module,
        "create_stage_table",
        lambda **kwargs: f"{kwargs['target_table']}__stage__ok",
    )
    monkeypatch.setattr(
        load_df_module, "validate_stage_target_key_overlap", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(load_df_module, "insert_from_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module,
        "insert_table_batch",
        lambda connection_type, connection_ref, table_name, batch, **kwargs: len(batch),
    )
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module,
        "cleanup_stage_table_with_retry",
        lambda connection_type, connection_key, connection_ref, table_name, **kwargs: (
            cleanups.append(
                (connection_type, connection_key, table_name),
            )
        ),
    )

    inserted_rows = load_df_module.load_df(
        "gp",
        "sandbox.target",
        pd.DataFrame({"id": [1, 2], "value": [10, 20]}),
        append=True,
        key_columns=["id"],
        progress=False,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert inserted_rows == 2
    assert cleanups == [("gp", "gp", "sandbox.target__stage__ok")]
