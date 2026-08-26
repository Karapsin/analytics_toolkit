from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    RecordingSourceConnection,
    SimpleNamespace,
    attempt_module,
    builtins,
    io,
    make_progress_options,
    models_module,
    parquet_stage_module,
    pd,
    pytest,
    uuid,
)


def test_create_parquet_stage_table_reports_collision_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(
        to_db_key="trino",
        to_db_backend="trino",
        transfer_staging_schema="scratch",
        s3_transfer_staging_schema="hive.scratch",
        s3_transfer_staging_location="s3://bucket/stage",
    )
    messages: list[str] = []
    monkeypatch.setattr(parquet_stage_module, "STAGE_TABLE_NAME_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(
        parquet_stage_module,
        "build_stage_table_name",
        lambda *_a, **_k: "scratch.collision",
    )
    monkeypatch.setattr(parquet_stage_module, "table_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(parquet_stage_module, "time_print", messages.append)

    with pytest.raises(RuntimeError, match="unique stage table"):
        parquet_stage_module.create_parquet_stage_table(
            options,
            models_module.TransferConnectionRefs(target={"connection": object()}),
            models_module.TransferStageState(
                target_exists=False,
                stage_column_types={"id": "BIGINT"},
            ),
        )
    assert len(messages) == 2


def test_create_parquet_stage_table_uses_staging_schema_and_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed_sqls: list[str] = []

    class FakeCursor:
        def execute(self, sql: str) -> None:
            executed_sqls.append(sql)

        def close(self) -> None:
            pass

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="trino",
        to_db_backend="trino",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        transfer_staging_schema="object_storage.sandbox",
        s3_transfer_staging_schema="hive.sandbox",
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
        transfer_staging_username="target_user",
        trino_mode="parquet",
        staging_ddl_properties={"compression_codec": "'ZSTD'"},
        parquet_ddl_properties={"parquet_marker": 7},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "BIGINT"},
    )

    monkeypatch.setattr(
        parquet_stage_module,
        "table_exists",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        parquet_stage_module.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="abcd1234"),
    )

    parquet_stage_module.create_parquet_stage_table(
        options=options,
        connection_refs=models_module.TransferConnectionRefs(
            target={"connection": FakeConnection()},
        ),
        stage_state=stage_state,
    )

    assert stage_state.stage_table == (
        "hive.sandbox.target__analytics_toolkit_target_user__stage__abcd1234"
    )
    assert stage_state.stage_external_location == (
        "s3://bucket/tmp/analytics_toolkit_transfer/target/"
        "__analytics_toolkit_target_user__stage__abcd1234/"
    )
    assert "compression_codec" not in executed_sqls[0]
    assert "parquet_marker = 7" in executed_sqls[0]
    assert executed_sqls == [
        "CREATE TABLE "
        "hive.sandbox.target__analytics_toolkit_target_user__stage__abcd1234 "
        "(\"id\" BIGINT) WITH (format = 'PARQUET',"
        "\n        external_location = 's3://bucket/tmp/analytics_toolkit_transfer/target/"
        "__analytics_toolkit_target_user__stage__abcd1234/',"
        "\n        parquet_marker = 7)"
    ]


@pytest.mark.parametrize(
    ("options_override", "stage_types", "message"),
    [
        ({"s3_transfer_staging_schema": None}, {"id": "BIGINT"}, "schema"),
        ({"s3_transfer_staging_location": None}, {"id": "BIGINT"}, "location"),
        ({}, None, "source schema"),
    ],
)
def test_create_parquet_stage_table_validates_required_inputs(
    options_override: dict[str, Any],
    stage_types: dict[str, str] | None,
    message: str,
) -> None:
    values: dict[str, Any] = {
        "to_db_key": "trino",
        "to_db_backend": "trino",
        "s3_transfer_staging_schema": "hive.scratch",
        "s3_transfer_staging_location": "s3://bucket/stage",
    }
    values.update(options_override)
    options = make_progress_options(**values)
    with pytest.raises(ValueError, match=message):
        parquet_stage_module.create_parquet_stage_table(
            options,
            models_module.TransferConnectionRefs(target={"connection": object()}),
            models_module.TransferStageState(
                target_exists=False,
                stage_column_types=stage_types,
            ),
        )


def test_keyed_parquet_writer_includes_slice_and_part_in_filename() -> None:
    batch = models_module.RowBatch(columns=["id"], rows=[(1,)])
    opened_uris: list[str] = []

    class FakeTable:
        @staticmethod
        def from_pydict(values: dict[str, list[Any]]) -> dict[str, list[Any]]:
            return values

    class FakePq:
        @staticmethod
        def write_table(
            _arrow_table: Any,
            spooled_file: Any,
            *,
            row_group_size: int,
        ) -> None:
            del row_group_size
            spooled_file.write(b"parquet")

    class FakeFsspec:
        def open(self, uri: str, mode: str) -> io.BytesIO:
            assert mode == "wb"
            opened_uris.append(uri)
            return io.BytesIO()

    inserted_rows = parquet_stage_module.write_batch_to_parquet_stage(
        batch,
        file_index=7,
        slice_index=3,
        stage_external_location="s3://bucket/tmp/stage/",
        pa=SimpleNamespace(Table=FakeTable),
        pq=FakePq,
        fsspec_module=FakeFsspec(),
        row_group_size=100,
    )

    assert inserted_rows == 1
    assert opened_uris == ["s3://bucket/tmp/stage/slice-00003-part-00007.parquet"]


def test_parquet_arrow_conversion_normalizes_uuid_values() -> None:
    batch = models_module.RowBatch(
        columns=["id"],
        rows=[(uuid.UUID("00000000-0000-0000-0000-000000000001"),), (None,)],
    )

    class FakeTable:
        @staticmethod
        def from_pydict(values: dict[str, list[Any]]) -> dict[str, list[Any]]:
            return values

    table = parquet_stage_module.row_batch_to_arrow_table(
        SimpleNamespace(Table=FakeTable),
        batch,
    )

    assert table == {"id": ["00000000-0000-0000-0000-000000000001", None]}


def test_parquet_arrow_conversion_preserves_timezone_in_iso_text() -> None:
    batch = models_module.RowBatch(
        columns=["event_ts"],
        rows=[(pd.Timestamp("2026-01-01 03:04:05.123456", tz="UTC"),)],
    )

    class FakeTable:
        @staticmethod
        def from_pydict(values: dict[str, Any]) -> dict[str, Any]:
            return values

    table = parquet_stage_module.row_batch_to_arrow_table(
        SimpleNamespace(Table=FakeTable),
        batch,
        column_types={"event_ts": "TIMESTAMP(6) WITH TIME ZONE"},
    )

    assert table == {"event_ts": ["2026-01-01 03:04:05.123456+00:00"]}


def test_parquet_arrow_conversion_types_all_null_columns() -> None:
    batch = models_module.RowBatch(
        columns=["event_ts", "custom_value"],
        rows=[(None, None), (None, None)],
    )

    class FakeTable:
        @staticmethod
        def from_pydict(values: dict[str, Any]) -> dict[str, Any]:
            return values

    fake_pa = SimpleNamespace(
        Table=FakeTable,
        array=lambda values, **kwargs: (values, kwargs["type"]),
        string=lambda: "string",
    )

    table = parquet_stage_module.row_batch_to_arrow_table(
        fake_pa,
        batch,
        column_types={
            "event_ts": "TIMESTAMP(6) WITH TIME ZONE",
            "custom_value": "CUSTOM_TYPE",
        },
    )

    assert table == {
        "event_ts": ([None, None], "string"),
        "custom_value": [None, None],
    }


@pytest.mark.parametrize(
    ("column_type", "expected"),
    [
        (None, None),
        ("TIMESTAMP(6)", "timestamp:us"),
        ("TIMESTAMP(6) WITH TIME ZONE", "string"),
        ("DECIMAL(12, 3)", "decimal:12:3"),
        ("BIGINT", "int64"),
        ("VARCHAR(20)", "string"),
        ("UNKNOWN", None),
    ],
)
def test_parquet_arrow_type_matches_trino_stage_type(
    column_type: str | None,
    expected: str | None,
) -> None:
    fake_pa = SimpleNamespace(
        binary=lambda: "binary",
        bool_=lambda: "bool",
        date32=lambda: "date32",
        decimal128=lambda precision, scale: f"decimal:{precision}:{scale}",
        float64=lambda: "float64",
        int64=lambda: "int64",
        string=lambda: "string",
        timestamp=lambda precision: f"timestamp:{precision}",
    )

    result = parquet_stage_module._arrow_type_for_trino_stage(fake_pa, column_type)

    assert result == expected


def test_parquet_dependencies_and_default_cleanup_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pa, pq, fsspec_module = parquet_stage_module.ensure_parquet_staging_dependencies()
    assert pa.__name__ == "pyarrow"
    assert pq.__name__ == "pyarrow.parquet"
    assert fsspec_module.__name__ == "fsspec"

    removed: list[tuple[str, bool]] = []
    fs = SimpleNamespace(rm=lambda path, recursive: removed.append((path, recursive)))
    fake_fsspec = SimpleNamespace(
        core=SimpleNamespace(url_to_fs=lambda _uri: (fs, "bucket/default"))
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "ensure_parquet_staging_dependencies",
        lambda: (object(), object(), fake_fsspec),
    )
    parquet_stage_module.cleanup_parquet_stage_location("s3://bucket/default")
    assert removed == [("bucket/default", True)]


def test_parquet_dependency_import_error_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fail_fsspec(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "fsspec":
            message = "missing fsspec"
            raise ImportError(message)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_fsspec)
    with pytest.raises(ImportError, match="pyarrow, fsspec, and s3fs"):
        parquet_stage_module.ensure_parquet_staging_dependencies()


def test_parquet_location_row_group_and_target_name_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        parquet_stage_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(
            parquet_stage_target_table_base=lambda table: table.replace(".", "_")
        ),
    )
    options = SimpleNamespace(
        s3_transfer_staging_location="s3://bucket/base/",
        transfer_staging_username=None,
        destination_table="schema.target",
    )
    assert (
        parquet_stage_module.build_stage_external_location(
            options,
            stage_suffix="fixed",
        )
        == "s3://bucket/base/schema_target/__analytics_toolkit_unknown__stage__fixed/"
    )
    assert parquet_stage_module.parquet_row_group_size(SimpleNamespace(batch_size=0)) == 1
    assert parquet_stage_module.parquet_row_group_size(SimpleNamespace(batch_size=60_000)) == 50_000
    with pytest.raises(ValueError, match="staging_location"):
        parquet_stage_module.build_stage_external_location(
            SimpleNamespace(s3_transfer_staging_location=None)
        )
    with pytest.raises(ValueError, match="target table"):
        parquet_stage_module._stage_target_table_name(SimpleNamespace())


def test_parquet_staging_missing_dependencies_raise_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(
        to_db_key="trino",
        to_db_backend="trino",
        transfer_staging_schema="object_storage.sandbox",
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
        trino_mode="parquet",
    )

    monkeypatch.setattr(
        attempt_module,
        "ensure_parquet_staging_dependencies",
        lambda: (_ for _ in ()).throw(
            ImportError(parquet_stage_module.PARQUET_STAGING_IMPORT_ERROR)
        ),
    )

    with pytest.raises(ImportError, match="pyarrow, fsspec, and s3fs"):
        attempt_module.load_stage_batches(
            options=options,
            connection_refs=models_module.TransferConnectionRefs(
                source={"connection": RecordingSourceConnection(rows=[(1,)])},
                target={"connection": object()},
            ),
            stage_state=models_module.TransferStageState(
                target_exists=False,
                stage_column_types={"id": "BIGINT"},
            ),
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )


def test_write_batch_to_parquet_stage_uses_one_spooled_file_without_getvalue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_spooled_files = 0
    max_active_spooled_files = 0
    uploads: list[tuple[str, Any]] = []

    class FakeSpooledFile:
        _rolled = True

        def __init__(self, max_size: int) -> None:
            nonlocal active_spooled_files, max_active_spooled_files
            assert max_size == parquet_stage_module.PARQUET_STAGE_MAX_SPOOL_BYTES
            active_spooled_files += 1
            max_active_spooled_files = max(
                max_active_spooled_files,
                active_spooled_files,
            )
            self.closed = False
            self.position = 0

        def seek(self, position: int) -> None:
            self.position = position

        def close(self) -> None:
            nonlocal active_spooled_files
            self.closed = True
            active_spooled_files -= 1

        def getvalue(self) -> bytes:
            raise AssertionError("Parquet staging must not materialize file bytes")

    monkeypatch.setattr(
        parquet_stage_module.tempfile,
        "SpooledTemporaryFile",
        FakeSpooledFile,
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "row_batch_to_arrow_table",
        lambda _pa, batch, **_kwargs: {"rows": list(batch.rows)},
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "write_arrow_table_to_parquet",
        lambda pq, arrow_table, spooled_file, row_group_size: None,
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "upload_spooled_file",
        lambda fsspec_module, spooled_file, remote_uri: uploads.append((remote_uri, spooled_file)),
    )

    row_count = parquet_stage_module.write_batch_to_parquet_stage(
        models_module.RowBatch(columns=["id"], rows=[(1,), (2,)]),
        file_index=0,
        stage_external_location="s3://bucket/tmp/stage/",
        pa=object(),
        pq=object(),
        fsspec_module=object(),
        row_group_size=2,
    )

    assert row_count == 2
    assert max_active_spooled_files == 1
    assert active_spooled_files == 0
    assert uploads[0][0] == "s3://bucket/tmp/stage/part-00000.parquet"
    assert uploads[0][1].closed is True


def test_write_dataframe_to_parquet_stage_chunks_progress_and_collects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads: list[str] = []
    progress: list[int] = []
    collected: list[bool] = []
    fake_pa = SimpleNamespace(
        Table=SimpleNamespace(from_pandas=lambda chunk, preserve_index: list(chunk["id"]))
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "write_arrow_table_to_parquet",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "upload_spooled_file",
        lambda _fs, _file, uri: uploads.append(uri),
    )
    monkeypatch.setattr(parquet_stage_module, "_spooled_file_rolled_to_disk", lambda _file: True)
    monkeypatch.setattr(parquet_stage_module.gc, "collect", lambda: collected.append(True))

    written = parquet_stage_module.write_dataframe_to_parquet_stage(
        pd.DataFrame({"id": [1, 2, 3]}),
        stage_external_location="s3://bucket/stage/",
        pa=fake_pa,
        pq=object(),
        fsspec_module=object(),
        row_group_size=2,
        on_progress=progress.append,
    )

    assert written == 3
    assert progress == [2, 1]
    assert uploads == [
        "s3://bucket/stage/part-00000.parquet",
        "s3://bucket/stage/part-00001.parquet",
    ]
    assert collected == [True, True]


def test_write_empty_parquet_batch_and_dataframe_are_noops() -> None:
    assert (
        parquet_stage_module.write_batch_to_parquet_stage(
            models_module.RowBatch(columns=["id"], rows=[]),
            file_index=0,
            stage_external_location="s3://bucket/stage",
            pa=object(),
            pq=object(),
            fsspec_module=object(),
            row_group_size=1,
        )
        == 0
    )
    assert (
        parquet_stage_module.write_dataframe_to_parquet_stage(
            pd.DataFrame(),
            stage_external_location="s3://bucket/stage",
            pa=object(),
            pq=object(),
            fsspec_module=object(),
            row_group_size=1,
        )
        == 0
    )
