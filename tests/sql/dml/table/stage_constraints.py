"""Complete typed stages are certified before any live SQL publication."""

from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.insert_constraints import (
    InsertConstraintError,
    read_staged_null_counts,
)
from analytics_toolkit.sql.dml.table import write_modes
from analytics_toolkit.sql.dml.table.insert_constraints import validate_staged_not_null_input
from analytics_toolkit.sql.dml.table.upsert_checkpoint import UpsertCheckpoint
from analytics_toolkit.sql.dml.table.write_modes import finalize_stage_table, upsert_stage_table
from sqlglot import exp, parse_one


def _connection(rows):
    results = iter(rows)
    queries = []
    closed = []
    connection = SimpleNamespace(queries=queries, closed=closed)
    connection.cursor = lambda: SimpleNamespace(
        execute=queries.append,
        fetchone=lambda: next(results),
        close=lambda: closed.append(True),
    )
    return connection


def _constraints(monkeypatch, backend, required, schemas=None):
    adapter = get_backend_adapter(backend)
    metadata = []

    def required_columns(_connection, table, *, connection_key):
        metadata.append((table, connection_key))
        return required

    monkeypatch.setattr(adapter, "get_required_column_defaults", required_columns)
    if schemas is not None:
        monkeypatch.setattr(
            adapter,
            "get_table_column_types",
            lambda _connection, table, **_kwargs: schemas[table],
        )
    return adapter, metadata


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_stage_constraint_probe_matches_native_casts_and_exact_identifier_identity(backend):
    adapter = get_backend_adapter(backend)
    connection = _connection([(3, 1)])
    count, nulls = read_staged_null_counts(
        adapter,
        connection,
        '"schema.with.dot"."stage"',
        {"target field": ("source field", "BIGINT")},
        query_label="constraint_probe",
    )
    assert (count, nulls) == (3, ("target field",))
    statement = parse_one(connection.queries[0], read=adapter.sqlglot_dialect)
    assert statement.find(exp.Table).db == "schema.with.dot"
    assert statement.find(exp.Table).name == "stage"
    assert len(list(statement.find_all(exp.Cast))) == 1
    assert 'CAST("source field" AS BIGINT) AS "source field"' in connection.queries[0]
    assert 'COUNT(*) - COUNT("source field")' in connection.queries[0]
    assert "query_label=constraint_probe" in connection.queries[0]
    assert connection.closed == [True]


@pytest.mark.parametrize("row", [None, (), (1, None), (1, -1), (1, 2), (True, 0), (1, 0, 0)])
def test_stage_constraint_probe_rejects_uncertifiable_results(row):
    connection = _connection([row])
    with pytest.raises(InsertConstraintError, match="Cannot certify") as error:
        read_staged_null_counts(
            get_backend_adapter("gp"), connection, "scratch.stage", {"id": ("id", "BIGINT")}
        )
    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert connection.closed == [True]


def test_all_owned_stage_payloads_validate_before_the_guard_returns(monkeypatch):
    _constraints(monkeypatch, "gp", {"id": False})
    connection = _connection([(1, 0), (1, 1)])
    with pytest.raises(InsertConstraintError, match="NOT NULL"):
        validate_staged_not_null_input(
            "gp",
            connection,
            "public.target",
            ["scratch.first", "scratch.first", "scratch.last"],
            columns=["id"],
            column_types={"id": "BIGINT"},
        )
    assert len(connection.queries) == 2
    assert '"scratch"."first"' in connection.queries[0]
    assert '"scratch"."last"' in connection.queries[1]
    assert connection.closed == [True, True]


@pytest.mark.parametrize("count", [0, 1])
def test_omitted_required_fields_use_actual_stage_count(monkeypatch, count):
    _constraints(monkeypatch, "gp", {"required": False})
    connection = _connection([(count,)])
    if count:
        with pytest.raises(InsertConstraintError, match="omits required"):
            validate_staged_not_null_input(
                "gp", connection, "public.target", ["scratch.stage"], columns=["optional"]
            )
    else:
        validate_staged_not_null_input(
            "gp", connection, "public.target", ["scratch.stage"], columns=["optional"]
        )
    assert "SELECT 1 FROM" in connection.queries[0]


@pytest.mark.parametrize("required", [{}, {"generated": True, "defaulted": True}])
def test_nullable_and_available_default_omissions_need_no_stage_scan(monkeypatch, required):
    _constraints(monkeypatch, "gp", required)
    connection = _connection([])
    validate_staged_not_null_input(
        "gp", connection, "public.target", ["scratch.stage"], columns=["optional"]
    )
    assert connection.queries == []


def test_legacy_positional_projection_uses_metadata_order_not_source_labels(monkeypatch):
    _adapter, metadata = _constraints(
        monkeypatch,
        "gp",
        {"target_id": False},
        {
            "public.target": {"target_id": "BIGINT", "target_text": "TEXT"},
            "scratch.stage": {"source_name": "BIGINT", "other_name": "TEXT"},
        },
    )
    connection = _connection([(1, 1)])
    with pytest.raises(InsertConstraintError, match="target_id"):
        validate_staged_not_null_input(
            "gp",
            connection,
            "public.target",
            ["scratch.stage"],
            columns=None,
            connection_key="chosen_alias",
        )
    assert metadata == [("public.target", "chosen_alias")]
    assert 'COUNT("source_name")' in connection.queries[0]
    assert "CAST(" not in connection.queries[0]


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_positional_stage_overflow_rejects_before_any_live_publication(monkeypatch, backend):
    adapter, metadata = _constraints(
        monkeypatch,
        backend,
        {"target_id": False},
        {
            "public.target": {"target_id": "BIGINT"},
            "scratch.stage": {"source_id": "BIGINT", "unexpected": "TEXT"},
        },
    )
    finalized = []
    monkeypatch.setattr(adapter, "finalize_stage_table", finalized.append)
    connection = _connection([])
    with pytest.raises(InsertConstraintError, match="more columns than the target") as error:
        finalize_stage_table(
            backend,
            connection,
            "scratch.stage",
            "public.target",
            False,
            True,
            pd.DataFrame(columns=["source_id", "unexpected"]),
            write_mode="truncate_insert",
        )
    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert metadata == [("public.target", backend)]
    assert connection.queries == []
    assert finalized == []


def test_legacy_append_default_mode_checks_before_calling_adapter(monkeypatch):
    adapter, _metadata = _constraints(monkeypatch, "gp", {"id": False})
    finalized = []
    monkeypatch.setattr(adapter, "finalize_stage_table", finalized.append)
    connection = _connection([(2, 1)])
    with pytest.raises(InsertConstraintError, match="NOT NULL"):
        finalize_stage_table(
            "gp",
            connection,
            "scratch.stage",
            "public.target",
            False,
            True,
            pd.DataFrame(columns=["id"]),
            insert_column_types={"id": "BIGINT"},
        )
    assert finalized == []


def test_actual_replacement_does_not_apply_the_old_constraint_schema(monkeypatch):
    adapter, metadata = _constraints(monkeypatch, "gp", {"old_id": False})
    finalized = []
    monkeypatch.setattr(adapter, "finalize_stage_table", finalized.append)
    connection = _connection([])
    finalize_stage_table(
        "gp", connection, "scratch.stage", "public.target", True, True, pd.DataFrame({"new": [1]})
    )
    assert metadata == []
    assert len(finalized) == 1


@pytest.mark.parametrize("null_count", [0, 1])
def test_prepared_partition_replay_validates_the_same_payload_before_submission(
    monkeypatch, null_count
):
    adapter, _metadata = _constraints(monkeypatch, "trino", {"id": False})
    commands = []
    monkeypatch.setattr(
        adapter, "execute_upsert_command", lambda *_args: commands.append(_args[-1])
    )
    checkpoint = UpsertCheckpoint(
        prepared=True,
        target_table="iceberg.public.target",
        final_stage_table="iceberg.scratch.payload",
        backend="trino",
        replay_sqls=[
            "DELETE FROM iceberg.public.target",
            "INSERT INTO iceberg.public.target SELECT 1",
        ],
        mutation_started=True,
    )
    connection = _connection([(2, null_count)])
    kwargs = {
        "columns": ["id"],
        "key_columns": ["id"],
        "column_types": {"id": "BIGINT"},
        "final_stage_table": checkpoint.final_stage_table,
        "checkpoint": checkpoint,
    }
    if null_count:
        with pytest.raises(InsertConstraintError, match="NOT NULL"):
            upsert_stage_table(
                "trino", connection, checkpoint.target_table, "iceberg.scratch.incoming", **kwargs
            )
        assert commands == []
    else:
        upsert_stage_table(
            "trino", connection, checkpoint.target_table, "iceberg.scratch.incoming", **kwargs
        )
        assert commands == checkpoint.replay_sqls
    assert '"iceberg"."scratch"."payload"' in connection.queries[0]
    assert checkpoint.prepared
    assert checkpoint.mutation_started


@pytest.mark.parametrize("null_count", [0, 1])
def test_partition_without_checkpoint_certifies_private_payload_before_delete(
    monkeypatch, null_count
):
    adapter, _metadata = _constraints(monkeypatch, "trino", {"id": False})
    commands = []
    statements = [
        "INSERT INTO scratch.payload SELECT id FROM public.target",
        "INSERT INTO scratch.payload SELECT id FROM scratch.incoming",
        "DELETE FROM public.target",
        "INSERT INTO public.target SELECT id FROM scratch.payload",
    ]
    monkeypatch.setattr(
        write_modes, "build_upsert_stage_sqls", lambda *_args, **_kwargs: statements
    )
    monkeypatch.setattr(
        adapter, "execute_upsert_command", lambda *_args: commands.append(_args[-1])
    )
    connection = _connection([(2, null_count)])
    kwargs = {
        "columns": ["id"],
        "key_columns": ["id"],
        "column_types": {"id": "BIGINT"},
        "final_stage_table": "scratch.payload",
    }
    if null_count:
        with pytest.raises(InsertConstraintError, match="NOT NULL"):
            upsert_stage_table("trino", connection, "public.target", "scratch.incoming", **kwargs)
        assert commands == statements[:2]
    else:
        upsert_stage_table("trino", connection, "public.target", "scratch.incoming", **kwargs)
        assert commands == statements
    assert '"scratch"."payload"' in connection.queries[0]


@pytest.mark.parametrize("alias", ["named_target", "gp"])
def test_partition_constraint_metadata_uses_the_actual_alias_namespace(
    monkeypatch, write_sql_connections, alias
):
    write_sql_connections(
        {
            alias: {
                "type": "trino",
                "host": "fake.invalid",
                "port": 8080,
                "user": "fake",
                "catalog": "catalog.with.dot",
                "schema": "schema space",
            },
        }
    )
    metadata = []
    cursor = SimpleNamespace(
        execute=lambda query, params: metadata.append((query, params)),
        fetchall=list,
        close=lambda: None,
    )
    commands = []
    monkeypatch.setattr(
        get_backend_adapter("trino"),
        "execute_upsert_command",
        lambda *_args: commands.append(_args[-1]),
    )
    checkpoint = UpsertCheckpoint(
        prepared=True,
        target_table="target",
        final_stage_table="payload",
        backend="trino",
        replay_sqls=["INSERT INTO target SELECT id FROM payload"],
    )
    upsert_stage_table(
        "trino",
        SimpleNamespace(cursor=lambda: cursor),
        "target",
        "incoming",
        columns=["id"],
        key_columns=["id"],
        final_stage_table="payload",
        checkpoint=checkpoint,
        connection_key=alias,
    )
    assert 'FROM "catalog.with.dot".information_schema.columns' in metadata[0][0]
    assert metadata[0][1] == ("schema space", "target")
    assert commands == checkpoint.replay_sqls
    checkpoint.target_table = '"catalog.with.dot"."schema space"."target"'
    checkpoint.final_stage_table = '"catalog.with.dot"."schema space"."payload"'
    commands.clear()
    upsert_stage_table(
        "trino",
        SimpleNamespace(cursor=lambda: cursor),
        checkpoint.target_table,
        "incoming",
        columns=["id"],
        key_columns=["id"],
        final_stage_table=checkpoint.final_stage_table,
        checkpoint=checkpoint,
    )
    assert metadata[1][1] == ("schema space", "target")
    assert commands == checkpoint.replay_sqls


def test_transfer_dry_run_leaves_existing_constraints_pending_with_explicit_types():
    plan = sql.transfer(
        "gp",
        "trino",
        from_sql="SELECT id FROM public.source",
        to_table="iceberg.public.target",
        write_mode="truncate_insert",
        table_schema={"id": "BIGINT"},
        dry_run=True,
    )
    assert plan.options["type_resolution_pending"] is False
    assert plan.options["target_constraints_validation_pending"] is True
    assert (
        plan.options["target_constraint_validation"]
        == "complete_owned_stage_before_live_publication"
    )
