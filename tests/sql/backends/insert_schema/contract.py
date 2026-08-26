from __future__ import annotations

from tests.sql._support.insert_schema import (
    Any,
    FakeClickHouseSourceAdapter,
    SimpleNamespace,
    SourceColumn,
    SqlConfigError,
    builtins,
    ch_source_count,
    ch_source_schema,
    connection_config,
    date,
    datetime,
    gp_config,
    pd,
    pytest,
    source_schema,
    timezone,
    trino_insert,
)


def test_chunk_rows_yields_all_chunks_and_stops() -> None:
    assert list(trino_insert._chunk_rows(iter([(1,), (2,), (3,)]), 2)) == [
        [(1,), (2,)],
        [(3,)],
    ]


@pytest.mark.parametrize(
    ("kind", "source_type", "precision", "scale", "expected"),
    [
        ("decimal", "numeric", None, None, "Decimal(38, 10)"),
        ("decimal", "numeric", 77, 2, "Decimal(38, 10)"),
        ("string", "unknown", None, None, "String"),
    ],
)
def test_clickhouse_base_type_fallbacks(
    kind: str,
    source_type: str,
    precision: int | None,
    scale: int | None,
    expected: str,
) -> None:
    assert (
        ch_source_schema._map_to_ch_base_type(
            kind,
            source_type,
            precision,
            scale,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("source_sql", "expected_rows", "enabled", "expected"),
    [
        ("SELECT * FROM source", 5, False, "SELECT * FROM source"),
        ("SELECT * FROM source", None, True, "SELECT * FROM source"),
        ("SELECT * FROM source", 0, True, "SELECT * FROM source"),
        ("SELECT * FROM source LIMIT 2", 5, True, "SELECT * FROM source LIMIT 2"),
        ("SELECT * FROM source;", 5, True, "SELECT * FROM source\nLIMIT 5"),
        ("invalid (", 5, True, "invalid (\nLIMIT 5"),
    ],
)
def test_clickhouse_count_limited_read(
    source_sql: str,
    expected_rows: int | None,
    enabled: bool,
    expected: str,
) -> None:
    assert (
        ch_source_count.source_sql_for_count_limited_read(
            FakeClickHouseSourceAdapter(),
            source_sql=source_sql,
            expected_rows=expected_rows,
            enabled=enabled,
        )
        == expected
    )


@pytest.mark.parametrize(("rows", "expected"), [([(7,)], 7), ([], 0)])
def test_clickhouse_count_source_rows(rows: list[Any], expected: int) -> None:
    connection = SimpleNamespace(query=lambda _sql: SimpleNamespace(result_rows=rows))

    assert (
        ch_source_count.count_source_rows(
            FakeClickHouseSourceAdapter(),
            connection,
            "SELECT * FROM source",
            query_label="q",
        )
        == expected
    )


def test_clickhouse_disables_query_limit_for_transfer_reads() -> None:
    assert ch_source_count.disable_query_limit_for_transfer_reads(object()) is True


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        ("binary", "Nullable(String)"),
        ("boolean", "Nullable(Bool)"),
        ("uint8", "Nullable(UInt8)"),
        ("uint16", "Nullable(UInt16)"),
        ("uint32", "Nullable(UInt32)"),
        ("uint64", "Nullable(UInt64)"),
        ("int8", "Nullable(Int8)"),
        ("smallint", "Nullable(Int16)"),
        ("integer", "Nullable(Int32)"),
        ("bigint", "Nullable(Int64)"),
        ("float32", "Nullable(Float32)"),
        ("double", "Nullable(Float64)"),
        ("decimal(20, 4)", "Nullable(Decimal(20, 4))"),
        ("date", "Nullable(Date)"),
        ("timestamp", "Nullable(DateTime64(6))"),
        ("uuid", "Nullable(UUID)"),
        ("Nullable(UUID)", "Nullable(UUID)"),
        ("varchar", "Nullable(String)"),
    ],
)
def test_clickhouse_maps_source_types(source_type: str, expected: str) -> None:
    column = SourceColumn("value", source_type)

    assert ch_source_schema.map_source_type_to_target(object(), column) == expected


def test_clickhouse_source_schema_inspection_and_refinement_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_columns = [SourceColumn("id", "Int64")]
    monkeypatch.setattr(
        ch_source_schema._source_schema,
        "inspect_clickhouse_source_schema",
        lambda connection, query: expected_columns,
    )
    monkeypatch.setattr(
        ch_source_schema._source_schema,
        "refine_clickhouse_column_types_nullability_from_rows",
        lambda column_types, columns, rows: {"id": "Int64"},
    )

    assert (
        ch_source_schema.inspect_source_query_schema(object(), "connection", "SELECT id")
        is expected_columns
    )
    assert ch_source_schema.refine_stage_column_types_from_rows(
        object(), {"id": "Nullable(Int64)"}, ["id"], [(1,)]
    ) == {"id": "Int64"}


def test_get_insert_chunk_size_falls_back_when_config_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_config(_connection_type: str) -> Any:
        message = "missing config"
        raise SqlConfigError(message)

    monkeypatch.setattr(connection_config, "get_connection_config", fail_config)

    assert trino_insert.get_insert_chunk_size(None, "missing") == 1000


def test_get_insert_chunk_size_prefers_explicit_value() -> None:
    assert trino_insert.get_insert_chunk_size(17) == 17


def test_get_insert_chunk_size_uses_only_trino_config_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTrinoConfig:
        def __init__(self, insert_chunk_size: int | None) -> None:
            self.insert_chunk_size = insert_chunk_size

    monkeypatch.setattr(connection_config, "TrinoConfig", FakeTrinoConfig)
    monkeypatch.setattr(
        connection_config,
        "get_connection_config",
        lambda connection_type: (
            FakeTrinoConfig(23)
            if connection_type == "configured"
            else SimpleNamespace(insert_chunk_size=99)
        ),
    )

    assert trino_insert.get_insert_chunk_size(None, "configured") == 23
    assert trino_insert.get_insert_chunk_size(None, "other") == 1000


def test_gp_config_reports_blocked_psycopg2_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "psycopg2":
            message = "blocked"
            raise ImportError(message)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(ImportError, match="required for Greenplum connections"):
        gp_config.open_connection(
            SimpleNamespace(),
            resolve_ca_certs=lambda *_args: None,
            resolve_single_cert_path=lambda *_args: None,
        )


def test_insert_dataframe_batch_delegates_normalized_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    batch = pd.DataFrame({"id": [1, 2], "value": ["a", "b"]})
    monkeypatch.setattr(
        trino_insert,
        "insert_rows",
        lambda *args, **kwargs: calls.append((*args, kwargs)),
    )

    trino_insert.insert_dataframe_batch(
        "adapter",
        "connection",
        "schema.target",
        batch,
        target_column_types={"id": "bigint"},
        trino_insert_chunk_size=10,
        query_label="q",
    )

    assert calls[0][0:3] == ("adapter", "connection", "schema.target")
    assert list(calls[0][3]) == ["id", "value"]
    assert calls[0][4] == [(1, "a"), (2, "b")]
    assert calls[0][-1]["trino_insert_chunk_size"] == 10


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), (pd.NA, True), ([1, 2], False), ("value", False)],
)
def test_is_null_like_handles_scalar_and_array_results(value: Any, expected: bool) -> None:
    assert trino_insert._is_null_like(value) is expected


def test_iter_dataframe_and_row_values_normalize_by_target_type() -> None:
    batch = pd.DataFrame({"id": ["1", "2"], "value": [3, None]})

    assert list(
        trino_insert.iter_dataframe_rows(
            batch,
            {"id": "bigint", "value": "varchar"},
        )
    ) == [(1, "3.0"), (2, None)]
    assert list(
        trino_insert.iter_row_values(
            ["id", "value"],
            [("4", [1, 2])],
            {"id": "bigint"},
        )
    ) == [(4, [1, 2])]


@pytest.mark.parametrize(
    ("value", "target_type", "expected"),
    [
        (None, None, "NULL"),
        (True, None, "TRUE"),
        (False, None, "FALSE"),
        (pd.Timestamp("NaT"), None, "NULL"),
        (
            pd.Timestamp("2026-01-02 03:04:05.123456"),
            "timestamp",
            "TIMESTAMP '2026-01-02 03:04:05.123456'",
        ),
        (pd.Timestamp("2026-01-02"), "date", "DATE '2026-01-02'"),
        (date(2026, 1, 2), "date", "DATE '2026-01-02'"),
        ("O'Reilly", None, "'O''Reilly'"),
        (2.5, None, "2.5"),
        (object(), None, None),
        (object(), "json", None),
    ],
)
def test_literal(value: Any, target_type: str | None, expected: str | None) -> None:
    result = trino_insert.literal(value, target_type)
    if expected is None:
        assert result.startswith("'") or result.startswith("CAST('")
    else:
        assert result == expected


@pytest.mark.parametrize(
    ("value", "target_type", "expected"),
    [
        (pd.NA, "bigint", None),
        (None, None, None),
        (3, "varchar(10)", "3"),
        (3, "CHAR", "3"),
        ("4", "bigint", 4),
        (
            datetime(2026, 1, 2, 3, 4, 5, 123456),
            "timestamp(3)",
            datetime(2026, 1, 2, 3, 4, 5, 123000),
        ),
        (
            datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc),
            "timestamp(0) with time zone",
            datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 1, 2, 3, 4, 5, 123456),
            "timestamp(6)",
            datetime(2026, 1, 2, 3, 4, 5, 123456),
        ),
        (
            datetime(2026, 1, 2, 3, 4, 5, 123456),
            "timestamp",
            datetime(2026, 1, 2, 3, 4, 5, 123456),
        ),
        (3.5, None, 3.5),
        ([1, 2], None, [1, 2]),
    ],
)
def test_normalize_value(value: Any, target_type: str | None, expected: Any) -> None:
    assert trino_insert.normalize_value(value, target_type) == expected


def test_shared_source_schema_clickhouse_nullability_refinement_edges() -> None:
    assert (
        source_schema.refine_clickhouse_column_types_nullability_from_rows(None, ["id"], [(1,)])
        is None
    )
    original = {"id": "Nullable(Int64)"}
    assert (
        source_schema.refine_clickhouse_column_types_nullability_from_rows(original, ["id"], [])
        is original
    )

    refined = source_schema.refine_clickhouse_column_types_nullability_from_rows(
        {"id": "Nullable(Int64)", "payload": "String", "unseen": "UInt8"},
        ["id", "payload"],
        [(1, None)],
    )
    assert refined == {"id": "Int64", "payload": "Nullable(String)", "unseen": "UInt8"}
    assert source_schema.is_null_value(None) is True
    assert source_schema.is_null_value([1, 2]) is False


def test_shared_source_schema_description_and_type_normalization_edges() -> None:
    description = SimpleNamespace(
        name="amount",
        type_code="decimal",
        precision="12",
        scale="2",
    )
    assert source_schema.source_column_from_description(
        description,
        type_code_name=lambda code, precision, scale: f"{code}({precision},{scale})",
    ) == SourceColumn("amount", "decimal(12,2)", precision=12, scale=2)
    assert source_schema.description_value(object(), "missing", 3) is None
    assert source_schema.optional_int("invalid") is None

    assert source_schema.normalize_type_name(None) == ""
    assert source_schema.normalize_type_name(" Nullable(LowCardinality(String)) ") == "string"
    assert source_schema.unwrap_type("string", "nullable") == "string"
    assert source_schema.classify_source_type("") == "string"
    assert source_schema.classify_source_type("geography") == "string"
