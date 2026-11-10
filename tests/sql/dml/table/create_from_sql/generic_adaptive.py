from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.dml import query_input
from analytics_toolkit.sql.dml.transfer.flow import adaptive_input
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch

create = importlib.import_module("analytics_toolkit.sql.dml.table.create_table_from_sql")


def disable_staging():
    path = Path(".connections")
    configs = json.loads(path.read_text())
    for config in configs.values():
        config["transfer_staging_schema"] = None
    path.write_text(json.dumps(configs))
    set_connections_path(path)


@pytest.fixture
def install(monkeypatch):
    disable_staging()

    def setup(backend, *, rows=((1,), (127,))):
        state = SimpleNamespace(
            events=[], inserts=[], schema=None, reads=0, inspections=0, fail_insert=False, rows=rows
        )
        connection = SimpleNamespace(close=lambda: None, rollback=lambda: None)
        monkeypatch.setattr(create, "get_sql_connection", lambda _key: connection)
        monkeypatch.setattr(create, "table_exists", lambda *_a: False)

        def inspect(*_args):
            state.inspections += 1
            assert state.inspections == 1, "certified source inspected again"
            return [SourceColumn("id", "BIGINT")]

        def drop(*_args, **kwargs):
            if kwargs["drop_target_if_exists"]:
                state.events.append("drop")
            return False

        def create_table(_backend, _connection, table, _frame, **kwargs):
            state.schema = kwargs["table_schema"]
            state.events.append("create")
            kwargs["on_resource_created"](table)

        def read(*_args, **_kwargs):
            state.reads += 1
            state.events.append("scan")
            for row in state.rows:
                yield RowBatch(["id"], [row])
            state.events.append("certified")

        def insert(_backend, _ref, _table, _columns, rows, **_kwargs):
            state.inserts.append(list(rows))
            if state.fail_insert and len(state.inserts) == 1:
                state.rows = ((999,),)
                message = "lost data acknowledgement"
                raise ConnectionError(message)
            return len(rows)

        monkeypatch.setattr(create, "inspect_source_query_schema", inspect)
        monkeypatch.setattr(create, "_create_sql_table_with_connection", create_table)
        monkeypatch.setattr(
            get_backend_adapter(backend), "prepare_existing_target_for_create_from_sql", drop
        )
        monkeypatch.setattr(adaptive_input, "iter_source_batches", read)
        monkeypatch.setattr(query_input, "insert_rows_batch", insert)
        monkeypatch.setattr(create, "_cleanup_attempt_target", lambda **_kwargs: True)
        monkeypatch.setattr(create, "insert_from_query", lambda *_a, **_k: 42)
        return state

    return setup


def run(backend, *, schema=None, source="SELECT id FROM sandbox.source", **kwargs):
    return create.create_table_from_sql(
        backend,
        "sandbox.events",
        source,
        table_schema=schema,
        retry_cnt=2,
        timeout_increment=0,
        return_metadata=True,
        **kwargs,
    )


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_generic_query_validates_all_values_before_authoritative_creation(install, backend):
    state = install(backend)
    result = run(backend, schema={"id": "SMALLINT"})
    assert state.events.index("certified") < state.events.index("create")
    assert state.inserts == [[(1,), (127,)]]
    assert result.rows == 2
    assert result.metadata.input_count == 2


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_generic_query_last_extreme_rejects_before_drop_or_create(install, backend):
    state = install(backend, rows=((1,), (32768,)))
    with pytest.raises(ValueError, match="authoritative integer"):
        run(backend, schema={"id": "SMALLINT"}, drop_target_if_exists=True)
    assert "drop" not in state.events
    assert "create" not in state.events
    assert not state.inserts


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_generic_self_source_is_frozen_before_destructive_creation(install, backend):
    state = install(backend)
    result = sql.create_table(
        backend,
        "sandbox.events",
        sql="SELECT id FROM sandbox.events",
        insert_data=True,
        drop_if_exists=True,
        return_metadata=True,
        retry_cnt=2,
        timeout_increment=0,
    )
    assert state.events.index("certified") < state.events.index("drop")
    assert state.inserts == [[(1,), (127,)]]
    assert result.rows == 2


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_generic_certified_retry_replays_same_rows_without_requery(install, backend):
    state = install(backend)
    state.fail_insert = True
    result = run(backend, schema={"id": "SMALLINT"})
    assert state.reads == 1
    assert state.inspections == 1
    assert state.inserts == [[(1,), (127,)], [(1,), (127,)]]
    assert result.rows == 2


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_generic_metadata_proven_copy_keeps_native_fast_path(install, backend):
    state = install(backend)
    result = run(backend)
    assert state.reads == 0
    assert state.inserts == []
    assert result.rows == 42


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_generic_query_dry_plan_defers_data_validation(install, backend):
    state = install(backend, rows=((32768,),))
    plan = run(backend, schema={"id": "SMALLINT"}, dry_run=True)
    assert plan.options["pending_input_validation"] is True
    assert state.events == []
    assert state.inspections == 0
