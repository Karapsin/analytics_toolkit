from __future__ import annotations

from tests.sql._support.cross_area import (
    FakeDbapiConnection,
    load_df_module,
    pd,
    pytest,
)


def test_load_df_dry_run_returns_ordered_labeled_plan() -> None:
    plan = load_df_module.load_df(
        "gp",
        "sandbox.scores",
        pd.DataFrame({"user_id": [1], "score": [10]}),
        write_mode="truncate_insert",
        dry_run=True,
        query_label="daily scores",
        gp_insert_chunk_size=5000,
    )

    assert plan.operation == "load_df"
    assert plan.target_alias == "gp"
    assert plan.options["gp_insert_chunk_size"] == 5000
    assert [statement.phase for statement in plan.statements] == [
        "clear_target",
        "create_target",
        "load_data",
        "analyze",
        "count_target",
    ]
    assert plan.sqls[0].startswith("/* analytics_toolkit query_label=daily scores */")
    assert "TRUNCATE TABLE sandbox.scores" in plan.sqls[0]


def test_load_df_dry_run_uses_table_schema() -> None:
    plan = load_df_module.load_df(
        "gp",
        "sandbox.scores",
        pd.DataFrame({"user_id": [1], "score": [10]}),
        dry_run=True,
        table_schema={"user_id": "TEXT", "score": "NUMERIC(8, 2)"},
    )

    create_sql = next(
        statement.sql for statement in plan.statements if statement.phase == "create_target"
    )
    assert plan.options["table_schema"] == {
        "user_id": "TEXT",
        "score": "NUMERIC(8, 2)",
    }
    assert '"user_id" TEXT' in create_sql
    assert '"score" NUMERIC(8, 2)' in create_sql


def test_load_df_upsert_dry_run_uses_backend_specific_sql() -> None:
    df = pd.DataFrame(
        {
            "id": [1],
            "sub_id": [None],
            "score": [10],
        }
    )

    gp_plan = load_df_module.load_df(
        "gp",
        "sandbox.scores",
        df,
        write_mode="upsert",
        key_columns=["id", "sub_id"],
        dry_run=True,
    )
    assert any("DELETE FROM sandbox.scores AS target_dst" in sql for sql in gp_plan.sqls)
    assert any(f"USING {gp_plan.metadata.stage_table} AS stage_src" in sql for sql in gp_plan.sqls)
    assert any(
        'target_dst."sub_id" IS NULL AND stage_src."sub_id" IS NULL' in sql for sql in gp_plan.sqls
    )
    assert any(
        'INSERT INTO sandbox.scores ("id", "sub_id", "score") '
        'SELECT "id", "sub_id", "score" FROM' in sql
        for sql in gp_plan.sqls
    )

    trino_plan = load_df_module.load_df(
        "trino",
        "sandbox.scores",
        df,
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        dry_run=True,
    )
    assert any(
        statement.phase == "create_final_upsert_stage" for statement in trino_plan.statements
    )
    assert any("SELECT target_dst." in sql for sql in trino_plan.sqls)
    assert any("DROP PARTITION" in sql for sql in trino_plan.sqls)
    assert not any(sql.startswith("MERGE INTO") for sql in trino_plan.sqls)


def test_load_df_passes_table_schema_to_create_sql_table(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    create_calls: list[dict[str, object]] = []

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: create_calls.append(kwargs),
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)

    inserted_rows = load_df_module.load_df(
        "gp",
        "sandbox.target",
        pd.DataFrame({"id": [1, 2], "amount": [1.5, 2.5]}),
        retry_cnt=1,
        timeout_increment=0,
        table_schema={"id": "TEXT", "amount": "NUMERIC(10, 2)"},
    )

    assert inserted_rows == 2
    assert create_calls[0]["table_schema"] == {
        "id": "TEXT",
        "amount": "NUMERIC(10, 2)",
    }


def test_load_df_return_metadata_preserves_rows_default_path(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    df = pd.DataFrame({"id": [1, 2], "value": ["a", "b"]})

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(load_df_module, "count_table_rows", lambda *args, **kwargs: 5)

    result = load_df_module.load_df(
        "gp",
        "sandbox.target",
        df,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == 2
    assert result.metadata.source_rows == 2
    assert result.metadata.inserted_rows == 2
    assert result.metadata.final_target_rows == 5


def test_load_df_upsert_requires_key_columns() -> None:
    with pytest.raises(ValueError, match="key_columns"):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1]}),
            write_mode="upsert",
            dry_run=True,
        )


def test_load_df_rejects_invalid_gp_insert_chunk_size() -> None:
    with pytest.raises(ValueError, match="gp_insert_chunk_size"):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1]}),
            gp_insert_chunk_size=0,
            dry_run=True,
        )

    with pytest.raises(ValueError, match="db_key has type 'gp'"):
        load_df_module.load_df(
            "trino",
            "sandbox.target",
            pd.DataFrame({"id": [1]}),
            gp_insert_chunk_size=100,
            dry_run=True,
        )


def test_load_df_clickhouse_dry_run_preserves_lifecycle_order_and_cluster() -> None:
    plan = load_df_module.load_df(
        "ch",
        "analytics.events",
        pd.DataFrame({"dt": ["2024-01-01"], "id": [1]}),
        write_mode="truncate_insert",
        dry_run=True,
        partition_by=["dt"],
        order_by=["dt", "id"],
        ch_cluster="analytics",
    )

    assert plan.statements[0].phase == "clear_target"
    assert plan.sqls[0] == ("TRUNCATE TABLE IF EXISTS analytics.events_shard ON CLUSTER analytics")
    assert plan.sqls[1] == "TRUNCATE TABLE IF EXISTS analytics.events"
    assert plan.statements[2].phase == "create_target"
    assert plan.sqls[2].startswith("CREATE TABLE IF NOT EXISTS analytics.events_shard")
    assert "ON CLUSTER analytics" in plan.sqls[2]


def test_load_df_clickhouse_only_shard_dry_run_uses_local_target() -> None:
    plan = load_df_module.load_df(
        "ch",
        "analytics.events",
        pd.DataFrame({"dt": ["2024-01-01"], "id": [1]}),
        write_mode="truncate_insert",
        ch_only_shard=True,
        dry_run=True,
        partition_by=["dt"],
        order_by=["dt", "id"],
        ch_cluster="analytics",
    )

    assert plan.options["ch_only_shard"] is True
    assert plan.sqls[0] == "TRUNCATE TABLE IF EXISTS analytics.events"
    create_sql = next(
        statement.sql for statement in plan.statements if statement.phase == "create_target"
    )
    assert create_sql.startswith("CREATE TABLE IF NOT EXISTS analytics.events\n")
    assert "ENGINE = ReplicatedMergeTree" in create_sql
    assert "PARTITION BY `dt`" in create_sql
    assert "ORDER BY (`dt`, `id`)" in create_sql
    assert "analytics.events_shard" not in "\n".join(plan.sqls)
    assert "ON CLUSTER analytics" in "\n".join(plan.sqls)
    assert "ENGINE = Distributed(" not in "\n".join(plan.sqls)
