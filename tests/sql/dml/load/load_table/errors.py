from __future__ import annotations

from sqlglot import exp, parse_one
from sqlparse import parse

from tests.sql._support.gp_upsert_load import native_upsert_load
from tests.sql._support.load_table import (
    FakeDbapiConnection,
    SimpleNamespace,
    gp_insert_module,
    load_df_module,
    load_sql_table_module,
    pd,
    pytest,
)
from tests.sql._support.transfer_schema import stage_module


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
    database = native_upsert_load(monkeypatch, {"id": "BIGINT", "value": "INTEGER"})
    original_rows = [dict(row) for row in database.target.rows]
    expected_stage = "public.target__stage__err"
    monkeypatch.setattr(stage_module, "build_stage_table_name", lambda *_a, **_k: expected_stage)
    failure = RuntimeError("insert failed")

    def fail_insert(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(load_df_module, "insert_table_batch", fail_insert)
    with pytest.raises(RuntimeError, match="insert failed") as raised:
        load_df_module.load_df(
            "gp",
            "public.target",
            pd.DataFrame({"id": [1, 2], "value": [10, 20]}),
            append=True,
            key_columns=["id"],
            progress=False,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert raised.value is failure
    drops = [
        (table.db, table.name)
        for command in database.commands
        if parse(command)[0].get_type() == "DROP"
        if isinstance((tree := parse_one(command, read="postgres")), exp.Drop)
        for table in tree.find_all(exp.Table)
    ]
    assert drops == [("public", expected_stage.split(".", 1)[1])]
    assert database.target.rows == original_rows
    assert set(database.tables) == {("public", "target")}


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
        destination_context={},
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


def test_load_df_upsert_existing_target_cleans_stage_on_finalization_error(monkeypatch) -> None:
    database = native_upsert_load(monkeypatch, {"id": "BIGINT", "score": "INTEGER"})
    database.target.rows = [{"id": 9, "score": 99}]
    original_rows = [dict(row) for row in database.target.rows]
    expected_stage = "public.target__stage__upsert"
    monkeypatch.setattr(stage_module, "build_stage_table_name", lambda *_a, **_k: expected_stage)
    failure = RuntimeError("merge failed")

    def fail_upsert(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(load_df_module, "upsert_stage_table", fail_upsert)
    with pytest.raises(RuntimeError, match="merge failed") as raised:
        load_df_module.load_df(
            "gp",
            "public.target",
            pd.DataFrame({"id": [1, 2], "score": [10, 20]}),
            write_mode="upsert",
            key_columns=["id"],
            retry_cnt=1,
            timeout_increment=0,
        )

    assert raised.value is failure
    drops = [
        (table.db, table.name)
        for command in database.commands
        if parse(command)[0].get_type() == "DROP"
        if isinstance((tree := parse_one(command, read="postgres")), exp.Drop)
        for table in tree.find_all(exp.Table)
    ]
    assert drops == [("public", expected_stage.split(".", 1)[1])]
    assert database.target.rows == original_rows
    assert set(database.tables) == {("public", "target")}
