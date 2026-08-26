from __future__ import annotations

from tests.sql._support.adapters import (
    UUID,
    Decimal,
    SimpleNamespace,
    SourceColumn,
    backend_validation_module,
    date,
    get_backend_adapter,
    pd,
    pytest,
)


def test_backend_validation_builds_multi_stage_duplicate_query() -> None:
    adapter = get_backend_adapter("gp")

    assert adapter.build_stage_duplicate_keys_sql_for_tables(
        ["stage.first", "stage.second"],
        ["id", "region"],
    ) == (
        "SELECT 1 FROM (\n"
        'SELECT "id", "region" FROM stage.first\n'
        "UNION ALL\n"
        'SELECT "id", "region" FROM stage.second\n'
        ') AS stage_src GROUP BY "id", "region" '
        "HAVING COUNT(*) > 1 LIMIT 1"
    )


def test_backend_validation_query_closes_cursor_when_execute_fails() -> None:
    query_error = RuntimeError("query failed")

    class Cursor:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, sql: str) -> None:
            raise query_error

        def close(self) -> None:
            self.closed = True

    cursor = Cursor()
    connection = SimpleNamespace(cursor=lambda: cursor)

    with pytest.raises(RuntimeError, match="query failed"):
        backend_validation_module.query_has_rows(
            get_backend_adapter("gp"),
            connection,
            "SELECT 1",
        )

    assert cursor.closed is True


def test_dataframe_column_type_inference_is_adapter_owned() -> None:
    gp_adapter = get_backend_adapter("gp")
    trino_adapter = get_backend_adapter("trino")
    ch_adapter = get_backend_adapter("ch")

    assert gp_adapter.infer_dataframe_column_type(pd.Series([1, 2])) == "BIGINT"
    assert gp_adapter.infer_dataframe_column_type(pd.Series([1.5, 2.5])) == "DOUBLE PRECISION"
    assert trino_adapter.infer_dataframe_column_type(pd.Series([1.5, 2.5])) == "DOUBLE"
    assert trino_adapter.infer_dataframe_column_type(pd.Series(["a", "b"])) == "VARCHAR"
    assert ch_adapter.infer_dataframe_column_type(pd.Series([1, None])) == ("Nullable(Float64)")
    assert (
        ch_adapter.infer_dataframe_column_type(pd.Series([Decimal("1.2"), Decimal("3.4")]))
        == "Float64"
    )


@pytest.mark.parametrize(
    ("series", "gp_type", "ch_type"),
    [
        (pd.Series([True, False]), "BOOLEAN", "Bool"),
        (
            pd.Series(pd.to_datetime(["2026-01-01", "2026-01-02"])),
            "TIMESTAMP",
            "DateTime64(6)",
        ),
        (pd.Series([date(2026, 1, 1), date(2026, 1, 2)]), "DATE", "Date"),
    ],
)
def test_dataframe_type_inference_covers_temporal_and_boolean_types(
    series: pd.Series,
    gp_type: str,
    ch_type: str,
) -> None:
    assert get_backend_adapter("gp").infer_dataframe_column_type(series) == gp_type
    assert get_backend_adapter("ch").infer_dataframe_column_type(series) == ch_type


def test_dataframe_type_inference_preserves_uuid_values() -> None:
    values = pd.Series([UUID(int=1), UUID(int=2)])
    nullable_values = pd.Series([UUID(int=1), None])
    mixed_values = pd.Series([UUID(int=1), "not-a-uuid"])

    assert get_backend_adapter("gp").infer_dataframe_column_type(values) == "UUID"
    assert get_backend_adapter("trino").infer_dataframe_column_type(values) == "UUID"
    assert get_backend_adapter("ch").infer_dataframe_column_type(values) == "UUID"
    assert get_backend_adapter("ch").infer_dataframe_column_type(nullable_values) == (
        "Nullable(UUID)"
    )
    assert get_backend_adapter("gp").infer_dataframe_column_type(mixed_values) == "TEXT"
    assert get_backend_adapter("trino").infer_dataframe_column_type(mixed_values) == "VARCHAR"
    assert get_backend_adapter("ch").infer_dataframe_column_type(mixed_values) == "String"


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("gp", "UUID"), ("trino", "UUID"), ("ch", "Nullable(UUID)")],
)
def test_source_type_mapping_preserves_uuid(backend: str, expected: str) -> None:
    assert (
        get_backend_adapter(backend).map_source_type_to_target(
            SourceColumn("value", "Nullable(UUID)")
        )
        == expected
    )
