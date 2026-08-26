from __future__ import annotations

from tests.sql._support.load_table import (
    Any,
    _write_trino_connections,
    load_df_module,
    pd,
    pytest,
)


def test_load_df_trino_parquet_dry_run_includes_stage_location(
    write_sql_connections: Any,
) -> None:
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
    )

    plan = load_df_module.load_df(
        "trino_stage",
        "iceberg.sandbox.target",
        pd.DataFrame({"id": [1], "label": ["a"]}),
        table_schema={"id": "BIGINT", "label": "VARCHAR"},
        dry_run=True,
    )

    assert plan.options["use_parquet_staging"] is True
    assert plan.metadata.stage_table == ("hive.pa_core_stage.daf6958bfec1c9f7__targetdryrun")
    assert plan.metadata.stage_external_location == (
        "s3://bucket/tmp/analytics_toolkit_transfer/target/"
        "__analytics_toolkit_target_user__stage__dryrun/"
    )
    assert [statement.phase for statement in plan.statements] == [
        "drop_target",
        "create_target",
        "create_stage",
        "load_stage",
        "insert_from_stage",
        "drop_stage",
        "cleanup_stage_location",
        "analyze",
        "count_target",
    ]
    assert any(
        "CREATE TABLE hive.pa_core_stage.daf6958bfec1c9f7__targetdryrun " in sql
        and "external_location = 's3://bucket/tmp/analytics_toolkit_transfer/target/" in sql
        for sql in plan.sqls
    )
    assert any(
        sql.startswith("WRITE PARQUET FILES TO s3://bucket/tmp/analytics_toolkit_transfer/target/")
        for sql in plan.sqls
    )
    assert any(sql.startswith("DELETE STAGE FILES s3://bucket/tmp/") for sql in plan.sqls)


def test_load_option_requirements_and_remaining_upsert_plan_branches(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Any,
) -> None:
    with pytest.raises(ValueError, match="destination_table"):
        load_df_module._build_load_options("gp", " ", False, None, None, None)
    with pytest.raises(ValueError, match="upsert_partition_column"):
        load_df_module._build_load_options(
            "trino",
            "sandbox.target",
            False,
            "upsert",
            None,
            ["id"],
        )

    write_sql_connections(
        {
            "trino_no_template": {
                "type": "trino",
                "host": "trino.example",
                "port": 8080,
                "user": "user",
                "password": "password",
                "catalog": "iceberg",
                "schema": "sandbox",
            }
        }
    )
    with pytest.raises(ValueError, match="drop_sql_template"):
        load_df_module._build_load_options(
            "trino_no_template",
            "sandbox.target",
            False,
            "upsert",
            None,
            ["id"],
            upsert_partition_column="event_date",
        )

    gp_upsert = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
        table_schema={"id": "BIGINT"},
        write_mode="upsert",
        key_columns=["id"],
        use_parquet_staging=True,
        transfer_staging_schema="scratch",
        s3_transfer_staging_location="s3://stage",
    )
    monkeypatch.setattr(load_df_module, "add_create_table_steps", lambda *_a, **_k: None)
    monkeypatch.setattr(load_df_module, "add_load_stage_step", lambda *_a, **_k: None)
    monkeypatch.setattr(load_df_module, "build_upsert_stage_sqls", lambda *_a, **_k: [])
    monkeypatch.setattr(load_df_module, "add_cleanup_stage_step", lambda *_a, **_k: None)
    load_df_module._add_parquet_load_plan_steps(
        load_df_module.SqlPlan(operation="load"),
        gp_upsert,
        pd.DataFrame({"id": [1]}),
        load_df_module.SqlOperationMetadata(),
    )

    monkeypatch.setattr(load_df_module, "validate_stage_uniqueness", lambda **_k: None)
    monkeypatch.setattr(load_df_module, "insert_from_table", lambda *_a, **_k: None)
    load_df_module._finalize_loaded_dataframe_stage(
        options=gp_upsert,
        state=load_df_module.LoadState(
            True,
            False,
            overlap_stage_table="scratch.stage",
        ),
        connection=object(),
        df=pd.DataFrame({"id": [1]}),
    )
