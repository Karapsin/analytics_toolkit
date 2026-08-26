from __future__ import annotations

from tests.sql._support.row_batches import (
    dry_run_module,
    make_gp_config,
    make_progress_options,
    make_trino_config,
    models_module,
    pytest,
    transfer_api_module,
)


def test_dry_run_fallback_names_locations_labels_and_source_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slices = [
        models_module.TransferSlice(i, (i,), f"id = {i}", f"select {i}", f"slice-{i}")
        for i in range(3)
    ]
    options = make_progress_options(
        target_table="schema.target",
        transfer_slices=slices,
        concurrency=2,
        s3_transfer_staging_location="s3://bucket/base/",
    )
    monkeypatch.setattr(
        dry_run_module,
        "build_stage_table_name",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad name")),
    )
    monkeypatch.setattr(
        dry_run_module,
        "build_stage_external_location",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad location")),
    )
    assert dry_run_module.dry_run_stage_table_names(options) == [
        "schema.target__stage__<runtime-transfer-id>__w00000",
        "schema.target__stage__<runtime-transfer-id>__w00001",
    ]
    assert dry_run_module.source_batches_label(options, 1).endswith("[1]")
    assert dry_run_module.source_batches_label(options) == "shared keyed source slice batches"
    assert dry_run_module.dry_run_stage_external_location(options) == (
        "s3://bucket/base/__stage__dryrun/"
    )
    assert (
        dry_run_module.infer_source_select_columns("delete from source", source_backend="gp")
        is None
    )
    assert (
        dry_run_module.infer_source_select_columns("select *, id from source", source_backend="gp")
        is None
    )
    assert (
        dry_run_module.infer_source_select_columns("select id + 1 from source", source_backend="gp")
        is None
    )
    assert dry_run_module.infer_source_select_columns("select from", source_backend="gp") is None


def test_transfer_dry_run_includes_estimate_total_rows_option() -> None:
    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        dry_run=True,
        estimate_total_rows=True,
        target_batch_memory_mb=32,
    )

    assert plan.options["estimate_total_rows"] is True
    assert plan.options["adaptive_batch_size_step"] == 0.1
    assert plan.options["target_rows_per_second_window"] == 5
    assert plan.options["target_rows_per_second_deadband"] == 0.15
    assert plan.options["target_batch_memory_mb"] == 32.0
    assert plan.options["max_batch_size"] is None
    assert plan.options["gp_insert_chunk_size"] is None
    assert plan.options["adaptive_gp_insert_chunk_size"] is False
    assert plan.options["initial_gp_insert_chunk_size"] is None

    plan = transfer_api_module.transfer_table(
        from_db="trino",
        to_db="gp",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        dry_run=True,
        target_rows_per_second_window=3,
        target_rows_per_second_deadband=0.05,
        adaptive_batch_size_step=0.25,
        gp_insert_chunk_size=50_000,
    )
    assert plan.options["target_rows_per_second_window"] == 3
    assert plan.options["target_rows_per_second_deadband"] == 0.05
    assert plan.options["adaptive_batch_size_step"] == 0.25
    assert plan.options["gp_insert_chunk_size"] == 50_000
    assert plan.options["adaptive_gp_insert_chunk_size"] is True
    assert plan.options["initial_gp_insert_chunk_size"] == 50_000

    plan = transfer_api_module.transfer_table(
        from_db="trino",
        to_db="gp",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        dry_run=True,
    )
    assert plan.options["gp_insert_chunk_size"] is None
    assert plan.options["adaptive_gp_insert_chunk_size"] is True
    assert plan.options["initial_gp_insert_chunk_size"] == 10_000


def test_transfer_dry_run_keyed_row_staging_uses_per_worker_stage_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "source": make_gp_config("source"),
        "target": make_gp_config("target"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="source",
        to_db="target",
        from_sql="select id, event_date from source_table where {event_date};",
        to_table="sandbox.target",
        table_schema={"id": "INTEGER", "event_date": "DATE"},
        transfer_keys="event_date",
        transfer_key_values=[f"2025-01-{day:02d}" for day in range(1, 80)],
        concurrency=5,
        dry_run=True,
    )

    phases = [statement.phase for statement in plan.statements]
    assert plan.options["transfer_slice_count"] == 79
    assert plan.options["worker_stage_count"] == 5
    assert plan.metadata.worker_stage_count == 5
    assert plan.metadata.stage_table == plan.metadata.aggregate_stage_table
    assert len(plan.metadata.stage_tables) == 5
    assert phases.count("read_source") == 79
    assert phases.count("create_stage") == 5
    assert phases.count("load_stage") == 5
    assert phases.count("consolidate_stage") == 4
    assert phases.count("insert_target") == 1
    assert phases.count("drop_stage") == 5
    assert all("<runtime-transfer-id>__w" in stage for stage in plan.metadata.stage_tables)
    assert all("39539a1e20d7e1c9__" in stage for stage in plan.metadata.stage_tables)
    assert any("worker 0 streamed keyed source slice batches [0, 5, 10" in sql for sql in plan.sqls)


def test_transfer_dry_run_reports_concurrency_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {"source": make_gp_config("source"), "target": make_gp_config("target")}
    monkeypatch.setattr(
        transfer_api_module, "get_connection_config", lambda db_key: configs[db_key]
    )

    plan = transfer_api_module.transfer_table(
        from_db="source",
        to_db="target",
        from_sql="select id from source_table where {event_date}",
        to_table="sandbox.target",
        table_schema={"id": "INTEGER"},
        transfer_keys="event_date",
        transfer_key_values=[1, 2, 3, 4],
        read_concurrency=8,
        write_concurrency=3,
        soft_concurrency_cap=2,
        hard_concurrency_cap=5,
        dry_run=True,
    )

    assert plan.options["requested_read_concurrency"] == 8
    assert plan.options["requested_write_concurrency"] == 3
    assert plan.options["soft_concurrency_cap"] == 2
    assert plan.options["hard_concurrency_cap"] == 5
    assert plan.options["soft_limited_read_concurrency"] == 2
    assert plan.options["soft_limited_write_concurrency"] == 2
    assert plan.options["effective_read_concurrency"] == 2
    assert plan.options["effective_write_concurrency"] == 2
    assert plan.options["source_connection_limit"] == 2
    assert plan.options["target_connection_limit"] == 2
    assert plan.metadata.requested_read_concurrency == 8
    assert plan.metadata.soft_limited_read_concurrency == 2
    assert plan.metadata.soft_limited_write_concurrency == 2
    assert plan.metadata.soft_concurrency_cap == 2
    assert plan.metadata.hard_concurrency_cap == 5


def test_transfer_dry_run_reports_split_concurrency_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {"source": make_gp_config("source"), "target": make_gp_config("target")}
    monkeypatch.setattr(
        transfer_api_module, "get_connection_config", lambda db_key: configs[db_key]
    )

    plan = transfer_api_module.transfer_table(
        from_db="source",
        to_db="target",
        from_sql="select id from source_table where {event_date}",
        to_table="sandbox.target",
        table_schema={"id": "INTEGER"},
        transfer_keys="event_date",
        transfer_key_values=[1, 2, 3, 4],
        read_concurrency=6,
        write_concurrency=2,
        hard_concurrency_cap=6,
        dry_run=True,
    )

    assert plan.options["concurrency"] is None
    assert plan.options["read_concurrency"] == 6
    assert plan.options["write_concurrency"] == 2
    assert plan.options["effective_read_concurrency"] == 4
    assert plan.options["effective_write_concurrency"] == 2
    assert plan.options["queue_capacity"] == 2
    assert plan.options["source_stage_phase_barrier"] is None
    assert plan.options["batch_queue_capacity_per_writer"] is None
    assert plan.options["reader_scheduling"] == "static_round_robin"
    assert plan.options["reader_slice_assignments"] == {
        0: [0],
        1: [1],
        2: [2],
        3: [3],
    }
    assert plan.options["target_stage_count"] == 2
    assert plan.metadata.requested_read_concurrency == 6
    assert plan.metadata.effective_write_concurrency == 2


def test_transfer_dry_run_reports_unkeyed_source_snapshot_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "source": make_gp_config("source", transfer_staging_schema="source_stage"),
        "target": make_gp_config("target", transfer_staging_schema="target_stage"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="source",
        to_db="target",
        from_table="sandbox.source_table",
        to_table="sandbox.target",
        table_schema={"id": "INTEGER"},
        dry_run=True,
    )

    assert plan.options["source_staging_mode"] == "source_staged"
    assert plan.options["source_stage_count"] == 1
    assert plan.options["source_stage_phase_barrier"] is True
    assert plan.options["source_stage_creation"] == "single_snapshot"
    assert plan.options["source_stage_lifecycle"] == "snapshot_then_stream_and_drop"
    assert plan.options["live_source_stage_limit"] is None
    assert plan.options["batch_queue_capacity_per_writer"] is None
    assert plan.metadata.source_staging_mode == "source_staged"
    assert plan.metadata.source_stage_count == 1


def test_transfer_dry_run_shows_from_table_source_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "source": make_gp_config("source"),
        "target": make_gp_config("target"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="source",
        to_db="target",
        from_table="sandbox.source_table",
        to_table="sandbox.target",
        table_schema={"id": "INTEGER"},
        dry_run=True,
    )

    assert plan.options["from_table"] == "sandbox.source_table"
    assert plan.options["source_table"] == "sandbox.source_table"
    read_source_sqls = [
        statement.sql for statement in plan.statements if statement.phase == "read_source"
    ]
    assert read_source_sqls == ["SELECT * FROM sandbox.source_table"]


def test_transfer_dry_run_shows_keyed_from_table_slice_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "source": make_gp_config("source"),
        "target": make_gp_config("target"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="source",
        to_db="target",
        from_table="sandbox.source_table",
        to_table="sandbox.target",
        table_schema={"id": "INTEGER", "event_date": "DATE"},
        transfer_keys="event_date",
        transfer_key_values=["2025-01-01", "2025-01-02"],
        concurrency=2,
        dry_run=True,
    )

    assert plan.options["from_table"] == "sandbox.source_table"
    assert plan.options["transfer_keys"] == ["event_date"]
    assert plan.options["transfer_key_expressions"] == {"event_date": "event_date"}
    assert plan.options["transfer_key_values"] == {"event_date": ["2025-01-01", "2025-01-02"]}
    assert plan.options["concurrency"] == 2
    assert plan.options["transfer_slice_count"] == 2
    read_source_sqls = [
        statement.sql for statement in plan.statements if statement.phase == "read_source"
    ]
    assert read_source_sqls == [
        "SELECT * FROM sandbox.source_table\nWHERE (event_date) = '2025-01-01'",
        "SELECT * FROM sandbox.source_table\nWHERE (event_date) = '2025-01-02'",
    ]


def test_transfer_dry_run_shows_keyed_slice_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "source": make_gp_config("source"),
        "target": make_gp_config("target"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="source",
        to_db="target",
        from_sql="select id, event_date from source_table where {event_date};",
        to_table="sandbox.target",
        table_schema={"id": "INTEGER", "event_date": "DATE"},
        transfer_keys="event_date",
        transfer_key_values=["2025-01-01", "2025-01-02"],
        concurrency=2,
        dry_run=True,
    )

    assert plan.options["transfer_keys"] == ["event_date"]
    assert plan.options["transfer_key_expressions"] == {"event_date": "event_date"}
    assert plan.options["transfer_key_values"] == {"event_date": ["2025-01-01", "2025-01-02"]}
    assert plan.options["concurrency"] == 2
    assert plan.options["transfer_slice_count"] == 2
    read_source_sqls = [
        statement.sql for statement in plan.statements if statement.phase == "read_source"
    ]
    assert len(read_source_sqls) == 2
    assert all("analytics_toolkit_transfer_source" not in sql for sql in read_source_sqls)
    assert all("SELECT *\nFROM (" not in sql for sql in read_source_sqls)
    assert read_source_sqls[0].startswith("select id, event_date from source_table where ")
    assert "(event_date) = '2025-01-01'" in read_source_sqls[0]
    assert "(event_date) = '2025-01-02'" in read_source_sqls[1]
    assert plan.options["worker_stage_count"] == 2
    assert plan.metadata.worker_stage_count == 2
    assert plan.metadata.aggregate_stage_table == plan.metadata.stage_tables[0]
    assert len(plan.metadata.stage_tables) == 2
    assert [statement.phase for statement in plan.statements].count("create_stage") == 2
    assert [statement.phase for statement in plan.statements].count("load_stage") == 2
    assert [statement.phase for statement in plan.statements].count("consolidate_stage") == 1
    assert [statement.phase for statement in plan.statements].count("drop_stage") == 2
    assert any("worker 0 streamed keyed source slice batches [0]" in sql for sql in plan.sqls)
    assert any("worker 1 streamed keyed source slice batches [1]" in sql for sql in plan.sqls)


def test_transfer_dry_run_shows_parquet_stage_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config("trino"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        write_mode="replace",
        table_schema={"id": "BIGINT"},
        dry_run=True,
    )

    assert plan.options["trino_mode"] == "parquet"
    assert "use_parquet_staging" not in plan.options
    assert plan.options["transferred_internal_columns"] == []
    assert "source-local" in plan.options["internal_columns"]
    assert plan.options["worker_stage_count"] == 1
    assert plan.metadata.worker_stage_count == 1
    assert plan.metadata.stage_tables == [plan.metadata.stage_table]
    assert plan.metadata.stage_table.startswith('hive.sandbox."39539a1e20d7e1c9__')
    assert "<runtime-transfer-id>__w00000" in plan.metadata.stage_table
    assert plan.metadata.stage_external_location == (
        "s3://bucket/tmp/analytics_toolkit_transfer/target/"
        "__analytics_toolkit_target_user__stage__<runtime-transfer-id>/"
    )
    assert any(
        sql.startswith('CREATE TABLE hive.sandbox."39539a1e20d7e1c9__')
        and "external_location = 's3://bucket/tmp/analytics_toolkit_transfer/target/" in sql
        for sql in plan.sqls
    )
    assert any(
        sql.startswith("WRITE PARQUET FILES TO ")
        and "__analytics_toolkit_target_user__stage__<runtime-transfer-id>/" in sql
        for sql in plan.sqls
    )
    assert "DROP TABLE IF EXISTS sandbox.target" in plan.sqls
    assert "DELETE FROM sandbox.target" not in plan.sqls
    assert any(sql.startswith("INSERT INTO sandbox.target") for sql in plan.sqls)
    assert any(sql.startswith("DROP TABLE IF EXISTS hive.sandbox") for sql in plan.sqls)
    assert any(sql.startswith("DELETE STAGE FILES s3://bucket/tmp") for sql in plan.sqls)


def test_transfer_dry_run_upsert_uses_parquet_stage_table_in_partition_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config("trino"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id, amount from source_table",
        to_table="sandbox.target",
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        table_schema={"id": "BIGINT", "amount": "DOUBLE"},
        dry_run=True,
    )

    assert any(
        sql.startswith("INSERT INTO ")
        and "__upsert" in sql
        and 'hive.sandbox."39539a1e20d7e1c9__' in sql
        for sql in plan.sqls
    )
    assert any("DROP PARTITION" in sql for sql in plan.sqls)


def test_transfer_dry_run_values_mode_uses_row_stage_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config("trino"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        table_schema={"id": "BIGINT"},
        trino_mode="values",
        dry_run=True,
    )

    assert plan.options["trino_mode"] == "values"
    assert "use_parquet_staging" not in plan.options
    assert plan.metadata.stage_external_location is None
    assert not any(sql.startswith("WRITE PARQUET FILES TO ") for sql in plan.sqls)
    assert not any(sql.startswith("DELETE STAGE FILES ") for sql in plan.sqls)
    assert any(
        sql.startswith("INSERT INTO ") and " SELECT * FROM (<source batches>)" in sql
        for sql in plan.sqls
    )
