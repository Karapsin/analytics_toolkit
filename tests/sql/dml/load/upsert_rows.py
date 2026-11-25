"""Complete-row and partition-scoped upsert contracts (audit 27-29)."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.dml.load.models import LoadOptions, LoadState
from analytics_toolkit.sql.dml.table.table_validation import (
    effective_upsert_key_columns,
    validate_complete_upsert_columns,
)
from analytics_toolkit.sql.dml.table.upsert_checkpoint import UpsertCheckpoint
from analytics_toolkit.sql.dml.table.write_modes import (
    build_upsert_stage_sqls,
    upsert_stage_table,
)
from sqlglot import parse_one

from tests.sql._support.fakes import FakeDbapiConnection
from tests.sql._support.gp_upsert_load import native_upsert_load
from tests.sql._support.load_table import load_df_module


def _patch_load_boundaries(monkeypatch, *, writable_types):
    adapter = get_backend_adapter("gp")
    connection = FakeDbapiConnection()
    inserted = []
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        adapter, "get_writable_table_column_types", lambda *_a, **_k: writable_types
    )
    monkeypatch.setattr(load_df_module, "get_table_column_types", lambda *_a, **_k: writable_types)
    monkeypatch.setattr(load_df_module, "create_stage_table", lambda **_k: "public.run_stage")
    monkeypatch.setattr(
        load_df_module,
        "insert_table_batch",
        lambda _backend, _ref, _table, batch, **_kwargs: (
            inserted.append(batch.copy()) or len(batch)
        ),
    )
    monkeypatch.setattr(load_df_module, "validate_stage_uniqueness", lambda **_k: None)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *_a, **_k: None)
    monkeypatch.setattr(load_df_module, "cleanup_stage_table_with_retry", lambda *_a, **_k: None)
    return connection, inserted


@pytest.mark.parametrize("columns", [["id"], ["id", "default_value", "unexpected"]])
def test_existing_upsert_rejects_missing_default_or_extra_before_stage(monkeypatch, columns):
    connection, inserted = _patch_load_boundaries(
        monkeypatch, writable_types={"id": "BIGINT", "default_value": "TEXT"}
    )
    with pytest.raises(ValueError, match="complete rows"):
        sql.load_df(
            "gp",
            "public.target",
            pd.DataFrame({name: [1] for name in columns}),
            write_mode="upsert",
            key_columns="id",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert not inserted
    assert not connection.executed


def test_reordered_complete_upsert_is_canonical_and_partition_scoped(monkeypatch):
    database = native_upsert_load(
        monkeypatch, {"id": "BIGINT", "month": "TEXT", "value": "INTEGER"}
    )
    database.target.rows = [
        {"id": 1, "month": "January", "value": -1},
        {"id": 1, "month": "March", "value": 30},
    ]
    count = sql.load_df(
        "gp",
        "public.target",
        pd.DataFrame({"value": [10, 20], "month": ["January", "February"], "id": [1, 1]}),
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="month",
        retry_cnt=1,
        timeout_increment=0,
    )
    assert count == 2
    assert list(database.inserted_frames[0]) == ["id", "month", "value"]
    deletion = next(statement for statement in database.commands if "DELETE FROM" in statement)
    assert 'target_dst."month" = stage_src."month"' in deletion
    assert 'target_dst."id" = stage_src."id"' in deletion
    # Private checkpoint CREATE/INSERT commits are separate from the one live
    # transactional key deletion and publication commit.
    assert database.target_transactions == [("DELETE", "INSERT")]
    assert sorted(database.target.rows, key=lambda row: row["month"]) == [
        {"id": 1, "month": "February", "value": 20},
        {"id": 1, "month": "January", "value": 10},
        {"id": 1, "month": "March", "value": 30},
    ]


def test_same_partition_key_is_duplicate_before_any_stage(monkeypatch):
    connection, inserted = _patch_load_boundaries(
        monkeypatch, writable_types={"id": "BIGINT", "month": "TEXT"}
    )
    with pytest.raises(ValueError, match="Duplicate key"):
        sql.load_df(
            "gp",
            "public.target",
            pd.DataFrame({"id": [1, 1], "month": ["Jan", "Jan"]}),
            write_mode="upsert",
            key_columns="id",
            upsert_partition_column="month",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert not inserted
    assert not connection.executed


def test_authoritative_smallint_checks_last_row_before_truncate(monkeypatch):
    connection, inserted = _patch_load_boundaries(monkeypatch, writable_types={"id": "SMALLINT"})
    monkeypatch.setattr(
        load_df_module, "get_table_column_types", lambda *_a, **_k: {"id": "SMALLINT"}
    )
    with pytest.raises(ValueError, match="authoritative integer"):
        sql.load_df(
            "gp",
            "public.target",
            pd.DataFrame({"id": [1, 2**40]}),
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert not inserted
    assert not connection.executed


def test_explicit_integer_schema_accepts_exact_integral_float(monkeypatch):
    _connection, inserted = _patch_load_boundaries(monkeypatch, writable_types={"id": "INTEGER"})
    assert (
        sql.load_df(
            "gp",
            "public.target",
            pd.DataFrame({"id": [1.0]}),
            append=True,
            table_schema={"id": "INTEGER"},
            retry_cnt=1,
            timeout_increment=0,
        )
        == 1
    )
    assert isinstance(inserted[0].iloc[0, 0], int)


def test_effective_key_deduplicates_partition_and_full_row_uses_target_order():
    assert effective_upsert_key_columns(["id", "month"], "month") == ["month", "id"]
    assert effective_upsert_key_columns(["id"]) == ["id"]
    assert validate_complete_upsert_columns(["value", "id"], {"id": "BIGINT", "value": "TEXT"}) == [
        "id",
        "value",
    ]


@pytest.mark.parametrize("backend", ["ch", "trino"])
def test_preservation_anti_join_scopes_keys_to_partition(backend):
    statements = build_upsert_stage_sqls(
        backend,
        "sandbox.target",
        "sandbox.incoming",
        columns=["id", "month", "value"],
        key_columns=["id"],
        upsert_partition_column="month",
        final_stage_table="sandbox.payload",
        partition_values=["Jan", "Feb"],
        trino_partition_drop_sql_template=(
            "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
        ),
    )
    predicate = statements[0].split("stage_src", 1)[1]
    quote = "`" if backend == "ch" else '"'
    assert f"target_dst.{quote}month{quote}" in predicate
    assert f"target_dst.{quote}id{quote}" in predicate


def test_clickhouse_generated_columns_are_not_writable():
    adapter = get_backend_adapter("ch")
    rows = [
        ("id", "UInt64", ""),
        ("value", "String", "DEFAULT"),
        ("computed", "UInt64", "MATERIALIZED"),
        ("alias", "UInt64", "ALIAS"),
    ]
    client = SimpleNamespace(query=lambda _sql: SimpleNamespace(result_rows=rows))
    assert adapter.get_writable_table_column_types(client, "db.target", connection_key="ch") == {
        "id": "UInt64",
        "value": "String",
    }


def test_key_validated_append_uses_explicit_subset_column_order(monkeypatch):
    connection, inserted = _patch_load_boundaries(
        monkeypatch, writable_types={"id": "BIGINT", "month": "TEXT", "value": "INTEGER"}
    )
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *_a, **_k: {
            "id": "BIGINT",
            "month": "TEXT",
            "value": "INTEGER",
        },
    )
    monkeypatch.setattr(load_df_module, "validate_stage_target_key_overlap", lambda **_k: None)
    assert (
        sql.load_df(
            "gp",
            "public.target",
            pd.DataFrame({"value": [10], "id": [7]}),
            append=True,
            key_columns="id",
            retry_cnt=1,
            timeout_increment=0,
        )
        == 1
    )
    assert list(inserted[0]) == ["value", "id"]
    final = next(
        statement for statement in connection.executed if "INSERT INTO public.target" in statement
    )
    assert 'INSERT INTO public.target ("value", "id")' in final
    assert 'SELECT CAST("value" AS INTEGER) AS "value", CAST("id" AS BIGINT) AS "id"' in final
    assert "SELECT *" not in final


def test_partition_replay_uses_certified_payload_without_recomputing_target(monkeypatch):
    adapter = get_backend_adapter("trino")
    connection = FakeDbapiConnection()
    calls = []
    failure = [True]

    def execute(_connection, statement):
        calls.append(statement)
        if "DROP PARTITION" in statement and failure[0]:
            failure[0] = False
            message = "partition mutation acknowledgement lost"
            raise ConnectionError(message)

    monkeypatch.setattr(adapter, "execute_command", execute)
    monkeypatch.setattr(adapter, "fetch_upsert_partition_values", lambda *_a, **_k: ["Jan"])
    checkpoint = UpsertCheckpoint()
    kwargs = {
        "columns": ["id", "month"],
        "key_columns": ["id"],
        "upsert_partition_column": "month",
        "final_stage_table": "sandbox.payload",
        "checkpoint": checkpoint,
        "trino_partition_drop_sql_template": (
            "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
        ),
    }
    with pytest.raises(ConnectionError):
        upsert_stage_table("trino", connection, "sandbox.target", "sandbox.incoming", **kwargs)
    assert checkpoint.prepared
    assert checkpoint.mutation_started
    upsert_stage_table("trino", connection, "sandbox.target", "sandbox.incoming", **kwargs)
    assert (
        len([statement for statement in calls if "FROM sandbox.target AS target_dst" in statement])
        == 1
    )
    assert len([statement for statement in calls if "DROP PARTITION" in statement]) == 2
    before = list(calls)
    with pytest.raises(ValueError, match="Prepared upsert payload"):
        upsert_stage_table("trino", connection, "sandbox.other", "sandbox.incoming", **kwargs)
    assert calls == before


def test_clickhouse_upsert_drops_verified_custom_physical_reference():
    adapter = get_backend_adapter("ch")

    def query(statement):
        if "custom_storage" in statement:
            ddl = "CREATE TABLE db.custom_storage (id UInt64) ENGINE=MergeTree ORDER BY id"
        else:
            ddl = (
                "CREATE TABLE db.events (id UInt64) "
                "ENGINE=Distributed('core', 'db', 'custom_storage', rand())"
            )
        return SimpleNamespace(result_rows=[(ddl,)])

    client = SimpleNamespace(query=query)
    statements = adapter.resolve_upsert_execution_sqls(
        client, "db.events", ["ALTER TABLE db.events_shard ON CLUSTER core DROP PARTITION 1"]
    )
    tree = parse_one(statements[0], read=ClickHouseDialect)
    assert tree.this.db == "db"
    assert tree.this.name == "custom_storage"
    assert "events_shard" not in statements[0]


def test_gp_character_last_row_rejects_before_truncate(monkeypatch):
    types = {"id": "CHARACTER(2)"}
    connection, inserted = _patch_load_boundaries(monkeypatch, writable_types=types)
    monkeypatch.setattr(load_df_module, "get_table_column_types", lambda *_a, **_k: types)
    with pytest.raises(ValueError, match="authoritative text length"):
        sql.load_df(
            "gp",
            "public.target",
            pd.DataFrame({"id": ["a", "long"]}),
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert not inserted
    assert not connection.executed


def test_load_retry_reuses_certified_upsert_payload_without_loading_again(monkeypatch):
    native_upsert_load(monkeypatch, {"id": "BIGINT"})
    loads = []
    replays = []

    def load(*, state, **_kwargs):
        loads.append(state)
        state.overlap_stage_table = "public.incoming"
        state.final_upsert_stage_table = "public.payload"
        state.upsert_checkpoint.prepared = True
        message = "lost data-write acknowledgement"
        raise ConnectionError(message)

    def replay(_backend, _connection, _target, incoming, **kwargs):
        replays.append((incoming, kwargs["checkpoint"]))
        assert kwargs["final_stage_table"] == "public.payload"

    monkeypatch.setattr(load_df_module, "_load_dataframe", load)
    monkeypatch.setattr(load_df_module, "upsert_stage_table", replay)
    assert (
        sql.load_df(
            "gp",
            "public.target",
            pd.DataFrame({"id": [1]}),
            write_mode="upsert",
            key_columns="id",
            retry_cnt=2,
            timeout_increment=0,
        )
        == 1
    )
    assert len(loads) == 1
    assert replays == [("public.incoming", loads[0].upsert_checkpoint)]


def test_certified_load_payload_requires_its_incoming_stage():
    options = LoadOptions("gp", "gp", "public.target")
    with pytest.raises(RuntimeError, match="no incoming stage"):
        load_df_module._resume_load_upsert(
            options, LoadState(True, True), pd.DataFrame({"id": [1]})
        )


def test_explicit_empty_replacement_reaches_target_creation(monkeypatch):
    connection, inserted = _patch_load_boundaries(monkeypatch, writable_types={"id": "BIGINT"})
    assert (
        sql.load_df(
            "gp",
            "public.target",
            pd.DataFrame({"id": pd.Series([], dtype="int64")}),
            table_schema={"id": "BIGINT"},
            empty_source_policy="replace",
            retry_cnt=1,
        )
        == 0
    )
    assert all(batch.empty for batch in inserted)
    assert any(
        "public.target" in command and "CREATE" in command for command in connection.executed
    )
