from __future__ import annotations

from tests.sql._support.load_table import (
    FakeDbapiConnection,
    SimpleNamespace,
    gp_insert_module,
    load_df_module,
    load_sql_table_module,
    pd,
    pytest,
)


def test_cleanup_load_reports_each_best_effort_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []
    monkeypatch.setattr(
        load_df_module,
        "_run_load_target_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )
    monkeypatch.setattr(
        load_df_module,
        "cleanup_parquet_stage_location",
        lambda location: (_ for _ in ()).throw(RuntimeError("delete failed")),
    )
    monkeypatch.setattr(load_df_module, "time_print", messages.append)

    load_df_module._cleanup_load(
        load_df_module.LoadOptions(
            connection_key="gp",
            connection_backend="gp",
            destination_table="sandbox.target",
        ),
        load_df_module.LoadState(
            target_exists=True,
            original_target_exists=False,
            target_created_by_operation=True,
            overlap_stage_table="sandbox.overlap_stage",
            final_upsert_stage_table="sandbox.final_stage",
            stage_external_location="s3://bucket/stage/",
        ),
        drop_created_target=True,
    )

    assert messages == [
        "Failed to drop temporary load_df stage table sandbox.overlap_stage",
        "Failed to drop temporary load_df final upsert stage table sandbox.final_stage",
        "Failed to drop load_df target table sandbox.target created by this failed operation",
        "Failed to delete temporary load_df Parquet stage files s3://bucket/stage/",
    ]


def test_insert_gp_rows_rolls_back_on_error(monkeypatch) -> None:
    connection = FakeDbapiConnection()

    def fake_execute_values(cursor, sql, rows, page_size):
        del cursor, sql, rows, page_size
        raise RuntimeError("insert failed")

    monkeypatch.setattr(gp_insert_module, "execute_values", fake_execute_values)

    with pytest.raises(RuntimeError, match="insert failed"):
        load_sql_table_module._insert_gp_rows(
            connection=connection,
            table_name="schema.stage_table",
            columns=["id"],
            rows=[(1,), (2,)],
            gp_insert_chunk_size=2,
        )

    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0


def test_load_df_drops_overlap_stage_table_on_error(monkeypatch) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    connection = FakeConnection()
    config = SimpleNamespace(
        connection_key="gp",
        backend="gp",
        user="target_user",
        transfer_staging_schema="transfer_schema",
        insert_chunk_size=None,
    )
    cleanups: list[tuple[str, str, str]] = []

    def fake_insert_table_batch(*args, **kwargs) -> int:
        del args, kwargs
        raise RuntimeError("insert failed")

    monkeypatch.setattr(load_df_module, "get_connection_config", lambda key: config)
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        load_df_module,
        "create_stage_table",
        lambda **kwargs: f"{kwargs['target_table']}__stage__err",
    )
    monkeypatch.setattr(
        load_df_module, "validate_stage_target_key_overlap", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(load_df_module, "insert_from_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(load_df_module, "insert_table_batch", fake_insert_table_batch)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module,
        "cleanup_stage_table_with_retry",
        lambda connection_type, connection_key, connection_ref, table_name, **kwargs: (
            cleanups.append(
                (connection_type, connection_key, table_name),
            )
        ),
    )

    with pytest.raises(RuntimeError, match="insert failed"):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1, 2], "value": [10, 20]}),
            append=True,
            key_columns=["id"],
            progress=False,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert cleanups == [("gp", "gp", "sandbox.target__stage__err")]


def test_load_df_failure_cleanup_drops_only_target_absent_at_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dropped: list[str] = []
    options = SimpleNamespace(
        connection_backend="gp",
        connection_key="target_db",
        destination_table="sandbox.target",
        retry_cnt=1,
        timeout_increment=1,
        query_label=None,
    )

    monkeypatch.setattr(
        load_df_module,
        "_run_load_target_action",
        lambda _options, _role, operation: operation({"connection": FakeDbapiConnection()}),
    )
    monkeypatch.setattr(
        load_df_module,
        "drop_table_with_retry",
        lambda _backend, _key, _ref, table_name, **_kwargs: dropped.append(table_name),
    )

    load_df_module._cleanup_load(
        options,
        load_df_module.LoadState(
            target_exists=True,
            original_target_exists=False,
            target_created_by_operation=True,
        ),
        drop_created_target=True,
    )
    load_df_module._cleanup_load(
        options,
        load_df_module.LoadState(
            target_exists=True,
            original_target_exists=True,
            target_created_by_operation=True,
        ),
        drop_created_target=True,
    )

    assert dropped == ["sandbox.target"]


def test_load_df_upsert_existing_target_cleans_stage_on_finalization_error(
    monkeypatch,
) -> None:
    connection = FakeDbapiConnection()
    cleanups: list[str] = []

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        load_df_module,
        "create_stage_table",
        lambda **kwargs: "sandbox.target__stage__upsert",
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "validate_stage_uniqueness", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module,
        "upsert_stage_table",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("merge failed")),
    )
    monkeypatch.setattr(
        load_df_module,
        "cleanup_stage_table_with_retry",
        lambda connection_type, connection_key, connection_ref, table_name, **kwargs: (
            cleanups.append(table_name)
        ),
    )

    with pytest.raises(RuntimeError, match="merge failed"):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1, 2], "score": [10, 20]}),
            write_mode="upsert",
            key_columns=["id"],
            retry_cnt=1,
            timeout_increment=0,
        )

    assert cleanups == ["sandbox.target__stage__upsert"]
