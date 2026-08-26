from __future__ import annotations

from tests.sql._support.transfer_schema import (
    FakeClickHouseClient,
    FakeDbapiConnection,
    SimpleNamespace,
    dt,
    get_backend_adapter,
    identifiers_module,
    pytest,
    runtime_models,
    schema_module,
    table_basic_module,
    transfer_finalize_module,
    transfer_stage_module,
)


def test_clickhouse_datetime64_timezone_is_attached_to_naive_batch_values() -> None:
    naive = dt.datetime(2026, 1, 2, 3, 4, 5, 123456)
    batch = runtime_models.RowBatch(
        columns=["event_ts", "label"],
        rows=[(naive, "unchanged")],
    )

    normalized = get_backend_adapter("ch").normalize_transfer_source_batch(
        batch,
        {"event_ts": "Nullable(DateTime64(6, 'UTC'))", "label": "String"},
    )

    assert normalized.rows[0][0].isoformat() == "2026-01-02T03:04:05.123456+00:00"
    assert normalized.rows[0][1] == "unchanged"
    assert batch.rows[0][0].tzinfo is None


def test_clickhouse_source_batch_timezone_normalization_handles_noop_paths() -> None:
    aware = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
    batch = runtime_models.RowBatch(columns=["event_ts"], rows=[(aware,)])
    adapter = get_backend_adapter("ch")

    unchanged_type = adapter.normalize_transfer_source_batch(
        batch,
        {"event_ts": "String"},
    )
    unchanged_value = adapter.normalize_transfer_source_batch(
        batch,
        {"event_ts": "DateTime64(6, 'UTC')"},
    )

    assert unchanged_type is batch
    assert unchanged_value.rows == [(aware,)]


def test_existing_target_insert_types_use_target_metadata() -> None:
    client = FakeClickHouseClient()

    result = schema_module.get_existing_target_insert_types(
        "ch",
        client,
        "target",
        {
            "id": "Nullable(Int32)",
            "amount": "Nullable(Float64)",
        },
        connection_key="ch",
    )

    assert result == {
        "id": "Nullable(Int64)",
        "amount": "Nullable(Decimal(18, 4))",
    }


def test_inspect_clickhouse_source_schema_is_adapter_owned() -> None:
    client = FakeClickHouseClient()

    result = schema_module.inspect_source_query_schema(
        "ch",
        client,
        "select id, amount from source;",
    )

    assert result == [
        schema_module.SourceColumn("id", "UInt64"),
        schema_module.SourceColumn("amount", "Nullable(Decimal(18, 4))"),
    ]
    assert client.queries == ["DESCRIBE TABLE (select id, amount from source)"]


def test_inspect_dbapi_source_schema_is_adapter_owned() -> None:
    connection = FakeDbapiConnection()

    result = schema_module.inspect_source_query_schema(
        "gp",
        connection,
        "select id, amount from source;",
    )

    assert result == [
        schema_module.SourceColumn("id", "integer"),
        schema_module.SourceColumn("amount", "numeric(12,2)", precision=12, scale=2),
    ]
    assert connection.cursor_obj.executed == [
        "SELECT * FROM (select id, amount from source) AS source_schema_probe WHERE 1 = 0"
    ]
    assert connection.cursor_obj.closed is True


def test_map_source_schema_to_target_preserves_binary_types() -> None:
    source_schema = [
        schema_module.SourceColumn("cheque_pk", "bytea"),
        schema_module.SourceColumn("raw_payload", "varbinary"),
    ]

    assert schema_module.map_source_schema_to_target(source_schema, "gp") == {
        "cheque_pk": "BYTEA",
        "raw_payload": "BYTEA",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "trino") == {
        "cheque_pk": "VARBINARY",
        "raw_payload": "VARBINARY",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "ch") == {
        "cheque_pk": "Nullable(String)",
        "raw_payload": "Nullable(String)",
    }


def test_map_source_schema_to_target_preserves_common_types() -> None:
    source_schema = [
        schema_module.SourceColumn("is_active", "boolean"),
        schema_module.SourceColumn("user_id", "integer"),
        schema_module.SourceColumn("amount", "numeric(12, 2)"),
        schema_module.SourceColumn("created_at", "timestamp"),
        schema_module.SourceColumn("payload", "jsonb"),
    ]

    assert schema_module.map_source_schema_to_target(source_schema, "gp") == {
        "is_active": "BOOLEAN",
        "user_id": "INTEGER",
        "amount": "NUMERIC(12, 2)",
        "created_at": "TIMESTAMP",
        "payload": "TEXT",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "trino") == {
        "is_active": "BOOLEAN",
        "user_id": "INTEGER",
        "amount": "DECIMAL(12, 2)",
        "created_at": "TIMESTAMP",
        "payload": "VARCHAR",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "ch") == {
        "is_active": "Nullable(Bool)",
        "user_id": "Nullable(Int32)",
        "amount": "Nullable(Decimal(12, 2))",
        "created_at": "Nullable(DateTime64(6))",
        "payload": "Nullable(String)",
    }


def test_map_source_schema_to_target_preserves_same_trino_native_types() -> None:
    source_schema = [
        schema_module.SourceColumn("campaign_codes", "array(varchar)"),
        schema_module.SourceColumn("po_bonus_pk", "array(varbinary)"),
        schema_module.SourceColumn("attributes", "map(varchar, bigint)"),
        schema_module.SourceColumn(
            "details",
            "row(campaign_code varchar, payload varbinary)",
        ),
        schema_module.SourceColumn("untyped_value", "unknown"),
    ]

    assert schema_module.map_source_schema_to_target(
        source_schema,
        "trino",
        source_backend="trino",
    ) == {
        "campaign_codes": "array(varchar)",
        "po_bonus_pk": "array(varbinary)",
        "attributes": "map(varchar, bigint)",
        "details": "row(campaign_code varchar, payload varbinary)",
        "untyped_value": "VARCHAR",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "trino") == {
        "campaign_codes": "VARCHAR",
        "po_bonus_pk": "VARCHAR",
        "attributes": "BIGINT",
        "details": "VARCHAR",
        "untyped_value": "VARCHAR",
    }


def test_refine_stage_column_types_for_clickhouse() -> None:
    result = schema_module.refine_stage_column_types_from_rows(
        "ch",
        {
            "dt": "Nullable(Date)",
            "amount": "Nullable(Decimal(12, 2))",
            "label": "String",
        },
        ["dt", "amount", "label"],
        [
            ("2024-01-01", None, "a"),
            ("2024-01-02", "1.20", "b"),
        ],
    )

    assert result == {
        "dt": "Date",
        "amount": "Nullable(Decimal(12, 2))",
        "label": "String",
    }


def test_refine_stage_column_types_noops_for_non_clickhouse_backends() -> None:
    column_types = {"id": "INTEGER"}

    assert (
        schema_module.refine_stage_column_types_from_rows(
            "gp",
            column_types,
            ["id"],
            [(None,)],
        )
        is column_types
    )


def test_table_basic_compatibility_delegates_and_identifier_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        table_basic_module._format_gp_information_schema_type(
            "numeric",
            "numeric",
            10,
            2,
        )
        == "NUMERIC(10, 2)"
    )
    assert table_basic_module._extract_row_count({"rows": 4}) == 4
    monkeypatch.setattr(
        identifiers_module.TableIdentifier,
        "parse",
        classmethod(lambda _cls, *_args: SimpleNamespace(parts=("one", "two", "three", "four"))),
    )
    with pytest.raises(Exception, match="up to three parts"):
        table_basic_module.quote_qualified_table_name("one.two.three.four", "trino")


def test_transfer_table_schema_overrides_clickhouse_nullability_refinement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_create_stage_table(**kwargs: object) -> str:
        captured.update(kwargs)
        return "target__stage"

    monkeypatch.setattr(
        transfer_stage_module,
        "create_stage_table",
        fake_create_stage_table,
    )

    options = runtime_models.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="ch",
        to_db_backend="ch",
        source_sql="select id from source_table",
        target_table="target",
        table_schema={"id": "String"},
    )
    stage_state = runtime_models.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "Nullable(Int64)"},
    )

    transfer_stage_module.initialize_stage_for_first_batch(
        options=options,
        connection_refs=runtime_models.TransferConnectionRefs(
            target={"connection": object()},
        ),
        stage_state=stage_state,
        batch=runtime_models.RowBatch(columns=["id"], rows=[(1,)]),
    )

    assert stage_state.stage_column_types == {"id": "String"}
    assert captured["column_types"] == {"id": "String"}
    assert stage_state.stage_table == "target__stage"


def test_transfer_upsert_duplicate_stage_keys_raise_before_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = runtime_models.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id, value from source_table",
        target_table="sandbox.target",
        write_mode="upsert",
        key_columns=["id"],
    )
    stage_state = runtime_models.TransferStageState(
        target_exists=True,
        stage_table="sandbox.target__stage",
        stage_table_created=True,
        first_non_empty_batch=runtime_models.RowBatch(
            columns=["id", "value"],
            rows=[(1, 10)],
        ).to_dataframe(include_rows=True),
        stage_column_types={"id": "BIGINT", "value": "INTEGER"},
    )
    finalized = False

    def fake_validate_stage_uniqueness(*args: object, **kwargs: object) -> None:
        raise ValueError("Duplicate key values found in staged data")

    def fake_finalize_stage_table(*args: object, **kwargs: object) -> None:
        nonlocal finalized
        finalized = True

    monkeypatch.setattr(
        transfer_finalize_module,
        "validate_stage_uniqueness",
        fake_validate_stage_uniqueness,
    )
    monkeypatch.setattr(
        transfer_finalize_module,
        "finalize_stage_table",
        fake_finalize_stage_table,
    )
    monkeypatch.setattr(
        transfer_finalize_module,
        "get_sql_connection",
        lambda key: object(),
    )

    with pytest.raises(ValueError, match="Duplicate key"):
        transfer_finalize_module.finalize_loaded_stage(
            options=options,
            connection_refs=runtime_models.TransferConnectionRefs(
                target={"connection": object()},
            ),
            stage_state=stage_state,
            total_rows=1,
        )

    assert finalized is False
