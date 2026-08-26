from __future__ import annotations

from tests.sql._support.backend_helpers import (
    UUID,
    Any,
    InvalidSqlInputError,
    RecordingConnection,
    SimpleNamespace,
    gp_adapter_module,
    gp_insert,
    gp_operations,
    importlib,
    pd,
    pytest,
    trino_operations,
    trino_parquet,
)


def test_gp_insert_normalization_and_dataframe_delegation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uuid_value = UUID(int=1)
    batch = pd.DataFrame(
        {
            "value": [1.0, float("nan")],
            "text": ["x", None],
            "uuid_value": [uuid_value, None],
        }
    )
    normalized = gp_insert.normalize_insert_batch(object(), batch)
    assert normalized.iloc[0].tolist() == [1.0, "x", str(uuid_value)]
    assert normalized.iloc[1].tolist() == [None, None, None]
    assert gp_insert.normalize_insert_rows(object(), [[pd.NA, uuid_value]]) == [
        (None, str(uuid_value))
    ]
    json_value = gp_insert.normalize_insert_rows(object(), [[{"nested": [1, 2]}]])[0][0]
    assert json_value.adapted == {"nested": [1, 2]}
    assert gp_insert._is_null_like([1, 2]) is False

    captured: dict[str, Any] = {}

    def fake_insert_rows(*args: Any, **kwargs: Any) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(gp_insert, "insert_rows", fake_insert_rows)
    gp_insert.insert_dataframe_batch(
        object(),
        object(),
        "public.target",
        pd.DataFrame({"id": [1, 2]}),
        gp_insert_chunk_size=2,
        query_label="load",
    )
    assert captured["args"][2:5] == (
        "public.target",
        pd.Index(["id"]),
        [(1,), (2,)],
    )
    assert captured["kwargs"]["gp_insert_chunk_size"] == 2


def test_gp_insert_rows_chunks_callbacks_and_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        build_dataframe_batch_insert_sql=lambda *_args, **_kwargs: "INSERT VALUES %s"
    )
    connection = RecordingConnection()
    pages: list[tuple[list[tuple[int, ...]], int]] = []
    progress: list[int] = []
    successes: list[tuple[float, int]] = []
    sizes = iter([2, 1])

    def fake_execute_values(
        _cursor: Any,
        _sql: str,
        rows: list[tuple[int, ...]],
        *,
        page_size: int,
    ) -> None:
        pages.append((rows, page_size))

    monkeypatch.setattr(gp_insert, "execute_values", fake_execute_values)
    gp_insert.insert_rows(
        adapter,
        connection,
        "target",
        ["id"],
        [[1], [2], [3]],
        page_size_getter=lambda: next(sizes),
        on_progress=progress.append,
        on_page_success=lambda duration, count: successes.append((duration, count)),
    )

    assert pages == [([(1,), (2,)], 2), ([(3,)], 1)]
    assert progress == [2, 1]
    assert [count for _, count in successes] == [2, 1]
    assert connection.commits == 1
    assert connection.cursor_instance.closed is True

    untouched = RecordingConnection()
    gp_insert.insert_rows(adapter, untouched, "target", ["id"], [])
    assert untouched.cursor_instance.closed is False


def test_gp_insert_rows_normalizes_json_array_values_by_target_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        build_dataframe_batch_insert_sql=lambda *_args, **_kwargs: "INSERT VALUES %s"
    )
    connection = RecordingConnection()
    captured_rows: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        gp_insert,
        "execute_values",
        lambda _cursor, _sql, rows, *, page_size: captured_rows.extend(rows),
    )

    gp_insert.insert_rows(
        adapter,
        connection,
        "target",
        ["payload", "array_value"],
        [[[3, {"x": "я"}], [1, 2]]],
        target_column_types={"payload": "JSONB", "array_value": "INTEGER[]"},
    )

    assert captured_rows[0][0].adapted == [3, {"x": "я"}]
    assert captured_rows[0][1] == [1, 2]
    assert gp_insert.normalize_json_columns(
        ["array_value"], [[[1, 2]]], {"array_value": "INTEGER[]"}
    ) == [([1, 2],)]


def test_gp_insert_rows_normalizes_json_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        build_dataframe_batch_insert_sql=lambda *_args, **_kwargs: "INSERT VALUES %s"
    )
    connection = RecordingConnection()
    captured_rows: list[tuple[Any, ...]] = []

    def fake_execute_values(
        _cursor: Any,
        _sql: str,
        rows: list[tuple[Any, ...]],
        *,
        page_size: int,
    ) -> None:
        del page_size
        captured_rows.extend(rows)

    monkeypatch.setattr(gp_insert, "execute_values", fake_execute_values)
    gp_insert.insert_rows(
        adapter,
        connection,
        "target",
        ["payload"],
        [[{"nested": [1, 2]}]],
    )

    assert captured_rows[0][0].adapted == {"nested": [1, 2]}


def test_gp_null_scalar_is_normalized_for_insert() -> None:
    assert gp_insert._is_null_like(None) is True
    assert gp_insert.normalize_insert_rows(object(), [[None]]) == [(None,)]


def test_gp_operation_backend_only_options_and_partition_requirements() -> None:
    with pytest.raises(InvalidSqlInputError, match="only supported for Trino"):
        gp_operations.build_show_tables_query(
            object(),
            object(),
            None,
            None,
            None,
            trino_catalog="hive",
        )
    with pytest.raises(InvalidSqlInputError, match="both start and end"):
        gp_operations.build_create_partition_sql(
            object(),
            "public.events",
            name="p1",
            start="2026-01-01",
        )


def test_greenplum_adapter_execution_and_type_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = gp_adapter_module.GreenplumAdapter()
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.general"),
        "time_print",
        lambda *_args, **_kwargs: None,
    )

    assert adapter.build_vacuum_table_sql("public.events", verbose=False) == (
        'VACUUM "public"."events"'
    )
    assert adapter.build_vacuum_table_sql(
        "public.events",
        analyze=True,
        full=True,
    ).startswith("VACUUM (FULL, VERBOSE, ANALYZE)")
    assert adapter.planned_execute_statements("SELECT 1; SELECT 2", gp_break_query=True) == [
        "SELECT 1",
        "SELECT 2",
    ]

    connection = RecordingConnection()
    adapter.execute_sql(
        connection,
        "SELECT 1; SELECT 2",
        print_queries=False,
        gp_break_query=True,
        gp_commit_each_statement=True,
        progress=False,
    )
    assert [sql for sql, _ in connection.cursor_instance.executed] == [
        "SELECT 1",
        "SELECT 2",
    ]
    assert connection.commits == 2

    assert adapter.type_code_name(None, None, None) is None
    assert adapter.type_code_name(1700, 12, 3) == "numeric(12,3)"
    assert adapter.type_code_name(99999, None, None) == "99999"
    assert adapter.type_code_name("custom", None, None) == "custom"
    assert "pid as query_id" in adapter.running_query_ids_sql()
    with pytest.raises(ValueError, match="backend PIDs"):
        adapter.normalize_query_id(True)
    with pytest.raises(ValueError, match="backend PIDs"):
        adapter.normalize_query_id("not-a-pid")
    assert adapter.cancel_status(pd.DataFrame({"cancelled": [False]})) == (
        False,
        "not_cancelled",
    )


def test_greenplum_adapter_insert_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = gp_adapter_module.GreenplumAdapter()
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    monkeypatch.setattr(
        adapter,
        "_insert_dataframe_batch",
        lambda *args, **kwargs: calls.append(("frame", args, kwargs)),
    )
    monkeypatch.setattr(
        adapter,
        "_insert_rows",
        lambda *args, **kwargs: calls.append(("rows", args, kwargs)),
    )
    frame = pd.DataFrame({"id": [1]})
    adapter.insert_dataframe_batch(
        object(),
        "target",
        frame,
        target_column_types=None,
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=4,
        connection_type="gp",
        query_label="batch",
        on_progress=None,
    )
    adapter.insert_rows_batch(
        object(),
        "target",
        ["id"],
        [[1]],
        target_column_types=None,
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=4,
        connection_type="gp",
        query_label="rows",
        on_progress=None,
    )
    assert [call[0] for call in calls] == ["frame", "rows"]
    assert adapter.normalize_insert_batch(frame).to_dict("list") == {"id": [1]}
    assert adapter.normalize_insert_rows([[pd.NA]]) == [(None,)]


@pytest.mark.parametrize(
    ("kind", "source_type", "precision", "scale", "expected"),
    [
        ("integer", "smallint", None, None, "SMALLINT"),
        ("integer", "uint32", None, None, "BIGINT"),
        ("integer", "uint64", None, None, "NUMERIC(20, 0)"),
        ("integer", "int64", None, None, "BIGINT"),
        ("float", "float32", None, None, "REAL"),
        ("float", "float64", None, None, "DOUBLE PRECISION"),
        ("date", "date", None, None, "DATE"),
        ("timestamp", "timestamptz", None, None, "TIMESTAMP WITH TIME ZONE"),
        ("timestamp", "timestamp", None, None, "TIMESTAMP"),
        ("unknown", "object", None, None, "TEXT"),
    ],
)
def test_map_to_gp_type_branches(
    kind: str,
    source_type: str,
    precision: int | None,
    scale: int | None,
    expected: str,
) -> None:
    assert gp_adapter_module._map_to_gp_type(kind, source_type, precision, scale) == expected


def test_trino_operation_modes_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(
        insert_chunk_size=5,
        s3_transfer_staging_location="s3://bucket/stage",
        upsert_partition_drop_sql_template="DELETE {partition}",
        catalog=None,
        connection_key="trino",
    )
    defaults = trino_operations.target_connection_defaults(object(), config)
    assert defaults.insert_chunk_size == 5
    assert defaults.s3_transfer_staging_location == "s3://bucket/stage"

    assert (
        trino_operations.resolve_transfer_staging_mode(
            object(),
            None,
            s3_transfer_staging_schema="stage",
            s3_transfer_staging_location="s3://bucket/stage",
        )
        == "parquet"
    )
    with pytest.raises(ValueError, match="requires s3_transfer_staging_schema"):
        trino_operations.resolve_transfer_staging_mode(
            object(),
            "parquet",
            s3_transfer_staging_schema=None,
            s3_transfer_staging_location="s3://bucket/stage",
        )
    with pytest.raises(ValueError, match="requires s3_transfer_staging_location"):
        trino_operations.resolve_transfer_staging_mode(
            object(),
            "parquet",
            s3_transfer_staging_schema="stage",
            s3_transfer_staging_location=None,
        )
    with pytest.raises(ValueError, match=r"requires.*catalog"):
        trino_operations.build_show_tables_query(object(), config, None, None, None)

    basic_ops = importlib.import_module("analytics_toolkit.sql.dml.table._basic_ops")
    monkeypatch.setattr(
        basic_ops,
        "get_trino_table_column_types",
        lambda *_args, **_kwargs: {"id": "bigint"},
    )
    assert trino_operations.resolve_transfer_stage_column_types(
        object(),
        object(),
        "stage.events",
        connection_key="trino",
        current_column_types=None,
    ) == {"id": "bigint"}


def test_trino_stage_type_reuse_and_parquet_null_edge_cases() -> None:
    current = {"id": "BIGINT"}
    assert (
        trino_operations.resolve_transfer_stage_column_types(
            object(),
            object(),
            "stage",
            connection_key="warehouse",
            current_column_types=current,
        )
        is current
    )
    adapter = SimpleNamespace(sqlglot_dialect="trino")
    with pytest.raises(ValueError, match="Invalid target table name"):
        trino_parquet.parquet_stage_target_table_base(adapter, "function_call()")
    assert trino_parquet._infer_trino_type_from_values([None, pd.NA, 3]) == "BIGINT"
    assert trino_parquet._infer_trino_type_from_values([[1, 2]]) == "VARCHAR"
