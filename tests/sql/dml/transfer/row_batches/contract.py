from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    Decimal,
    FakeTransferConnection,
    SimpleNamespace,
    StaticClickHouseClient,
    attempt_module,
    date,
    datetime,
    estimate_module,
    keys_module,
    make_gp_config,
    make_progress_options,
    models_module,
    pytest,
    row_counts_module,
    transfer_api_module,
)


def test_clickhouse_estimator_skips_non_simple_select() -> None:
    connection = StaticClickHouseClient([("default", "source_table", 1, 789, 1)])
    options = models_module.TransferOptions(
        from_db_key="ch",
        from_db_backend="ch",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table where id > 10",
        target_table="sandbox.target",
        estimate_total_rows=True,
    )

    assert estimate_module.estimate_source_rows(options, connection) is None
    assert connection.queries == []


def test_clickhouse_transfer_does_not_add_count_limit_for_empty_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_conn = FakeTransferConnection("source")
    target_conn = FakeTransferConnection("target")
    streamed_sql: list[str] = []
    options = make_progress_options(
        to_db_key="target_db",
        to_db_backend="trino",
        from_db_key="source_db",
        from_db_backend="ch",
        source_sql="select distinct magnit_id from source_table",
        validate_row_count=True,
        ch_count_limit_read=True,
    )

    monkeypatch.setattr(
        attempt_module,
        "get_sql_connection",
        lambda connection_key: source_conn if connection_key == "source_db" else target_conn,
    )
    monkeypatch.setattr(
        attempt_module,
        "create_stage_state",
        lambda *_args, **_kwargs: models_module.TransferStageState(target_exists=False),
    )
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        lambda *_args, **_kwargs: [
            SimpleNamespace(name="magnit_id", native_type="UInt64", precision=None, scale=None)
        ],
    )
    monkeypatch.setattr(attempt_module, "ensure_transfer_target_table", lambda *a, **k: None)

    def fake_load_stage_batches(**kwargs: Any) -> int:
        streamed_sql.append(kwargs["options"].source_sql)
        kwargs["stage_state"].stage_table = "iceberg.sandbox.target__stage__abcd1234"
        return 0

    monkeypatch.setattr(attempt_module, "load_stage_batches", fake_load_stage_batches)
    monkeypatch.setattr(attempt_module, "finalize_loaded_stage", lambda *a, **k: None)
    monkeypatch.setattr(attempt_module, "cleanup_stage", lambda *a, **k: None)
    monkeypatch.setattr(attempt_module, "close_connection_ref", lambda *a, **k: None)
    monkeypatch.setattr(row_counts_module, "count_source_rows", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(row_counts_module, "count_table_rows", lambda *_args, **_kwargs: 0)

    total_rows = attempt_module.run_transfer_attempt(
        options=options,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 0
    assert streamed_sql == ["select distinct magnit_id from source_table"]


def test_clickhouse_transfer_streams_with_count_limit_when_source_has_no_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_conn = FakeTransferConnection("source")
    target_conn = FakeTransferConnection("target")
    streamed_sql: list[str] = []
    options = make_progress_options(
        to_db_key="target_db",
        to_db_backend="trino",
        from_db_key="source_db",
        from_db_backend="ch",
        source_sql="select distinct magnit_id from source_table",
        validate_row_count=True,
        ch_count_limit_read=True,
    )

    monkeypatch.setattr(
        attempt_module,
        "get_sql_connection",
        lambda connection_key: source_conn if connection_key == "source_db" else target_conn,
    )
    monkeypatch.setattr(
        attempt_module,
        "create_stage_state",
        lambda *_args, **_kwargs: models_module.TransferStageState(target_exists=False),
    )
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        lambda *_args, **_kwargs: [
            SimpleNamespace(name="magnit_id", native_type="UInt64", precision=None, scale=None)
        ],
    )
    monkeypatch.setattr(attempt_module, "ensure_transfer_target_table", lambda *a, **k: None)

    def fake_load_stage_batches(**kwargs: Any) -> int:
        streamed_sql.append(kwargs["options"].source_sql)
        kwargs["stage_state"].stage_table = "iceberg.sandbox.target__stage__abcd1234"
        return 6_582_921

    monkeypatch.setattr(attempt_module, "load_stage_batches", fake_load_stage_batches)
    monkeypatch.setattr(attempt_module, "finalize_loaded_stage", lambda *a, **k: None)
    monkeypatch.setattr(attempt_module, "cleanup_stage", lambda *a, **k: None)
    monkeypatch.setattr(attempt_module, "close_connection_ref", lambda *a, **k: None)
    monkeypatch.setattr(
        row_counts_module,
        "count_source_rows",
        lambda *_args, **_kwargs: 6_582_921,
    )
    monkeypatch.setattr(row_counts_module, "count_table_rows", lambda *_args, **_kwargs: 6_582_921)

    total_rows = attempt_module.run_transfer_attempt(
        options=options,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 6_582_921
    assert streamed_sql == ["select distinct magnit_id from source_table\nLIMIT 6582921"]


def test_transfer_ignore_source_staging_uses_direct_keyed_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "source": make_gp_config("source", transfer_staging_schema="source_stage"),
        "target": make_gp_config("target"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    options = transfer_api_module.build_transfer_options(
        from_db="source",
        to_db="target",
        from_sql="select id from source_table where {event_date}",
        to_table="sandbox.target",
        transfer_keys="event_date",
        transfer_key_values=[1, 2],
        read_concurrency=2,
        ignore_source_staging=True,
    )

    assert options.ignore_source_staging is True
    assert options.source_transfer_staging_schema is None
    assert options.transfer_concurrency.effective_read == 2


def test_transfer_row_count_result_sorts_slice_dictionaries() -> None:
    result = models_module.TransferRowCountResult(
        expected_source_rows=3,
        streamed_rows=3,
        stage_rows=3,
        row_count_validated=True,
        slice_counts=[
            models_module.TransferSliceRowCount(2, "second", 2, 2),
            models_module.TransferSliceRowCount(1, None, 1, 1),
        ],
    )

    assert result.slice_counts_as_dicts() == [
        {
            "index": 1,
            "label": None,
            "expected_rows": 1,
            "streamed_rows": 1,
        },
        {
            "index": 2,
            "label": "second",
            "expected_rows": 2,
            "streamed_rows": 2,
        },
    ]


def test_transfer_slice_query_literals_and_inline_placeholders() -> None:
    values = (
        "Bob's",
        date(2025, 1, 1),
        datetime(2025, 1, 1, 12, 30, 1),
        7,
        1.5,
        True,
        Decimal("10.25"),
        None,
    )
    transfer_slice = keys_module.build_transfer_slice(
        index=3,
        source_sql=(
            "select * from events where {name} and {dt} and {ts} and {id} "
            "and {score} and {active} and {amount} and {deleted_at}"
        ),
        transfer_keys=[
            keys_module.TransferKey(name="name", expression="name"),
            keys_module.TransferKey(name="dt", expression="dt"),
            keys_module.TransferKey(name="ts", expression="ts"),
            keys_module.TransferKey(name="id", expression="id"),
            keys_module.TransferKey(name="score", expression="score"),
            keys_module.TransferKey(name="active", expression="active"),
            keys_module.TransferKey(name="amount", expression="amount"),
            keys_module.TransferKey(name="deleted_at", expression="deleted_at"),
        ],
        values=values,
    )

    assert transfer_slice.source_sql.startswith("select * from events where ")
    assert "SELECT *\nFROM (" not in transfer_slice.source_sql
    assert "(name) = 'Bob''s'" in transfer_slice.predicate_sql
    assert "(dt) = DATE '2025-01-01'" in transfer_slice.predicate_sql
    assert "(ts) = TIMESTAMP '2025-01-01 12:30:01'" in transfer_slice.predicate_sql
    assert "(id) = 7" in transfer_slice.predicate_sql
    assert "(score) = 1.5" in transfer_slice.predicate_sql
    assert "(active) = TRUE" in transfer_slice.predicate_sql
    assert "(amount) = 10.25" in transfer_slice.predicate_sql
    assert "(deleted_at) IS NULL" in transfer_slice.predicate_sql
    assert "\n  AND " in transfer_slice.predicate_sql
