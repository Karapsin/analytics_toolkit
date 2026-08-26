from __future__ import annotations

from tests.sql._support.cross_area import (
    Any,
    inspect,
    models_module,
    pytest,
    sql_module,
    transfer_api_module,
)


def test_transfer_upsert_dry_run_uses_delete_insert_or_merge() -> None:
    gp_plan = transfer_api_module.transfer_table(
        from_db="trino",
        to_db="gp",
        from_sql="select id, score from source_table",
        to_table="sandbox.scores",
        write_mode="upsert",
        key_columns=["id"],
        table_schema={"id": "BIGINT", "score": "INTEGER"},
        dry_run=True,
    )
    assert any("DELETE FROM sandbox.scores AS target_dst" in sql for sql in gp_plan.sqls)
    assert any(
        'INSERT INTO sandbox.scores ("id", "score") SELECT CAST("id" AS BIGINT)' in sql
        for sql in gp_plan.sqls
    )

    trino_plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id, score from source_table",
        to_table="sandbox.scores",
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        table_schema={"id": "BIGINT", "score": "INTEGER"},
        dry_run=True,
    )
    assert any(
        statement.phase == "create_final_upsert_stage" for statement in trino_plan.statements
    )
    assert any("DROP PARTITION" in sql for sql in trino_plan.sqls)
    assert not any(sql.startswith("MERGE INTO") for sql in trino_plan.sqls)

    ch_plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="ch",
        from_sql="select id, score from source_table",
        to_table="analytics.scores",
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        table_schema={"id": "UInt64", "score": "Int64"},
        ch_cluster="analytics",
        dry_run=True,
    )
    assert any(
        "ALTER TABLE analytics.scores_shard ON CLUSTER analytics DROP PARTITION" in sql
        for sql in ch_plan.sqls
    )
    assert not any(sql.startswith("DELETE FROM analytics.scores") for sql in ch_plan.sqls)
    assert any("INSERT INTO analytics.scores" in sql for sql in ch_plan.sqls)


def test_transfer_upsert_dry_run_infers_source_columns_without_table_schema() -> None:
    trino_plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id, score from source_table",
        to_table="sandbox.scores",
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        dry_run=True,
    )

    final_insert_sql = next(
        statement.sql
        for statement in trino_plan.statements
        if statement.phase == "upsert_target"
        and statement.sql.startswith("INSERT INTO ")
        and 'SELECT "id", "score" FROM' in statement.sql
    )
    assert 'SELECT CAST("id" AS BIGINT)' not in final_insert_sql
    assert 'SELECT "id", "score" FROM' in final_insert_sql

    gp_plan = transfer_api_module.transfer_table(
        from_db="trino",
        to_db="gp",
        from_sql="select id, score from source_table",
        to_table="sandbox.scores",
        write_mode="upsert",
        key_columns=["id"],
        dry_run=True,
    )

    assert any(
        'INSERT INTO sandbox.scores ("id", "score") SELECT "id", "score" FROM' in sql
        for sql in gp_plan.sqls
    )


def test_transfer_upsert_dry_run_uses_placeholder_for_unknown_source_columns() -> None:
    plan = transfer_api_module.transfer_table(
        from_db="trino",
        to_db="gp",
        from_sql="select * from source_table",
        to_table="sandbox.scores",
        write_mode="upsert",
        key_columns=["id"],
        dry_run=True,
    )

    assert any("<source query columns>" in sql for sql in plan.sqls)
    assert not any('INSERT INTO sandbox.scores ("id")' in sql for sql in plan.sqls)


def test_transfer_upsert_requires_key_columns() -> None:
    with pytest.raises(ValueError, match="key_columns"):
        transfer_api_module.transfer_table(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            write_mode="upsert",
            dry_run=True,
        )


def test_transfer_dry_run_includes_source_stage_and_target_steps() -> None:
    signature = inspect.signature(sql_module.transfer)

    assert "replace_target_table" not in signature.parameters
    assert signature.parameters["write_mode"].default == "append"
    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        dry_run=True,
        query_label="copy-target",
    )

    assert plan.operation == "transfer_table"
    assert plan.source_alias == "gp"
    assert plan.target_alias == "trino"
    assert plan.options["adaptive_batch_size"] is True
    assert plan.options["write_mode"] == "append"
    assert plan.options["min_batch_size"] == 1_000
    assert plan.options["max_batch_size"] == 400_000
    assert plan.options["target_batch_seconds"] == 10.0
    assert plan.statements[0].phase == "read_source"
    assert not {"clear_target", "drop_target"} & {statement.phase for statement in plan.statements}
    assert "query_label=copy-target" in plan.statements[0].sql
    assert plan.statements[-1].phase == "drop_stage"


def test_transfer_rejects_removed_replace_target_table_argument() -> None:
    with pytest.raises(TypeError, match="replace_target_table"):
        transfer_api_module.transfer_table(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            replace_target_table=True,
        )


def test_transfer_table_dry_run_uses_table_schema() -> None:
    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id, amount from source_table",
        to_table="sandbox.target",
        dry_run=True,
        table_schema={"id": "VARCHAR", "amount": "DECIMAL(10, 2)"},
    )

    create_sqls = [
        statement.sql
        for statement in plan.statements
        if statement.phase in {"create_stage", "create_target"}
    ]
    assert plan.options["table_schema"] == {
        "id": "VARCHAR",
        "amount": "DECIMAL(10, 2)",
    }
    assert len(create_sqls) == 2
    assert all('"id" VARCHAR' in sql for sql in create_sqls)
    assert all('"amount" DECIMAL(10, 2)' in sql for sql in create_sqls)


def test_transfer_table_logs_source_sql_preview(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        transfer_api_module,
        "run_transfer_attempt",
        lambda **kwargs: 3,
    )

    result = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="\n\nselect id from source_table",
        to_table="sandbox.target",
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=1,
        full_timeout_increment=0,
        progress=False,
    )

    output = capsys.readouterr().out
    assert result == 3
    assert "[transfer_table] [trino/trino] [transfer] Finished SQL in " in output
    assert "Finished SQL statement:\nselect id from source_table" in output


def test_keyed_source_staged_transfer_suppresses_source_sql_preview(
    monkeypatch,
    capsys,
) -> None:
    secret = "credential-secret"
    error = RuntimeError("keyed transfer failed")

    def fail_attempt(**_kwargs: Any) -> int:
        raise error

    monkeypatch.setattr(transfer_api_module, "run_transfer_attempt", fail_attempt)

    with pytest.raises(RuntimeError) as caught:
        transfer_api_module.transfer_table(
            from_db="ch",
            to_db="trino",
            from_sql=(
                f"SELECT id FROM source_table WHERE {{partition_id}} AND api_token = '{secret}'"
            ),
            to_table="sandbox.target",
            transfer_keys="partition_id",
            transfer_key_values=[1],
            retry_cnt=1,
            timeout_increment=0,
            full_retry_cnt=1,
            full_timeout_increment=0,
            progress=False,
        )

    assert caught.value is error
    assert caught.value.sql_context.sql_preview is None
    output = capsys.readouterr().out
    assert "Finished SQL statement" not in output
    assert secret not in output


def test_transfer_return_metadata_includes_row_count_validation(monkeypatch) -> None:
    def fake_run_transfer_attempt(**kwargs: Any) -> int:
        object.__setattr__(
            kwargs["options"],
            "row_count_result",
            models_module.TransferRowCountResult(
                expected_source_rows=3,
                streamed_rows=3,
                stage_rows=3,
                row_count_validated=True,
            ),
        )
        return 3

    monkeypatch.setattr(transfer_api_module, "run_transfer_attempt", fake_run_transfer_attempt)
    monkeypatch.setattr(transfer_api_module, "count_table_rows", lambda *args, **kwargs: 3)

    result = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=1,
        full_timeout_increment=0,
        return_metadata=True,
    )

    assert result.metadata.expected_source_rows == 3
    assert result.metadata.streamed_rows == 3
    assert result.metadata.stage_rows == 3
    assert result.metadata.row_count_validated is True


def test_transfer_clickhouse_dry_run_preserves_drop_pair_cluster() -> None:
    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="ch",
        from_sql="select id from source_table",
        to_table="analytics.events",
        write_mode="replace",
        dry_run=True,
        ch_cluster="analytics",
    )

    drop_sqls = [statement.sql for statement in plan.statements if statement.phase == "drop_target"]
    assert drop_sqls == [
        "DROP TABLE IF EXISTS analytics.events",
        "DROP TABLE IF EXISTS analytics.events_shard",
        "DROP TABLE IF EXISTS analytics.events ON CLUSTER analytics",
        "DROP TABLE IF EXISTS analytics.events_shard ON CLUSTER analytics",
    ]


def test_transfer_clickhouse_only_shard_dry_run_uses_local_target_sql() -> None:
    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="ch",
        from_sql="select dt, id from source_table",
        to_table="analytics.events",
        write_mode="replace",
        ch_only_shard=True,
        dry_run=True,
        table_schema={"dt": "Date", "id": "UInt64"},
        partition_by=["dt"],
        order_by=["dt", "id"],
        ch_cluster="analytics",
    )

    assert plan.options["ch_only_shard"] is True
    drop_sqls = [statement.sql for statement in plan.statements if statement.phase == "drop_target"]
    assert drop_sqls == ["DROP TABLE IF EXISTS analytics.events"]
    target_create_sql = [
        statement.sql
        for statement in plan.statements
        if statement.phase == "create_target" and statement.target_table == "analytics.events"
    ][0]
    assert target_create_sql.startswith("CREATE TABLE IF NOT EXISTS analytics.events\n")
    assert "ENGINE = ReplicatedMergeTree" in target_create_sql
    assert "PARTITION BY `dt`" in target_create_sql
    assert "ORDER BY (`dt`, `id`)" in target_create_sql
    assert "analytics.events_shard" not in "\n".join(plan.sqls)
    assert "ON CLUSTER analytics" in "\n".join(plan.sqls)
    assert "ENGINE = Distributed(" not in "\n".join(plan.sqls)
