from __future__ import annotations

import datetime as dt
import importlib
import json
from functools import partial
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


def record_native_copy(state, _backend, _connection, table, query, schema, **_kwargs):
    state.native_copies.append((table, query, schema))
    state.events.append("native_copy")
    return 42


@pytest.fixture
def install(monkeypatch):
    disable_staging()

    def setup(backend, *, rows=((1,), (127,)), native_type="BIGINT"):
        state = SimpleNamespace(
            events=[],
            inserts=[],
            native_copies=[],
            schema=None,
            reads=0,
            inspections=0,
            fail_insert=False,
            forbid_read=False,
            rows=rows,
        )
        connection = SimpleNamespace(close=lambda: None, rollback=lambda: None)
        monkeypatch.setattr(create, "get_sql_connection", lambda _key: connection)
        monkeypatch.setattr(create, "table_exists", lambda *_a: False)

        def inspect(*_args):
            state.inspections += 1
            assert state.inspections == 1, "certified source inspected again"
            return [SourceColumn("id", native_type)]

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
            assert not state.forbid_read, "native SQL copy must not read Python calendar values"
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
        monkeypatch.setattr(create, "insert_from_query", partial(record_native_copy, state))
        return state

    return setup


def run(backend, *, schema=None, source="SELECT id FROM sandbox.source", retry_cnt=2, **kwargs):
    return create.create_table_from_sql(
        backend,
        "sandbox.events",
        source,
        table_schema=schema,
        retry_cnt=retry_cnt,
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


def test_trino_native_calendar_sql_copy_keeps_exact_schema_without_python_source_reads(install):
    native = "TIMESTAMP(6) WITH TIME ZONE"
    value = dt.datetime(2024, 1, 1, 12, 0, 0, 123456, tzinfo=dt.timezone.utc)
    state = install("trino", rows=((value,),), native_type=native)
    state.forbid_read = True

    result = run("trino", retry_cnt=1)

    assert result.rows == 42
    assert state.reads == 0
    assert state.inspections == 1
    assert state.inserts == []
    assert state.schema == {"id": native}
    assert state.native_copies == [
        ("sandbox.events", "SELECT id FROM sandbox.source", {"id": native})
    ]
    assert state.events == ["create", "native_copy"]


def test_trino_self_source_calendar_copy_still_freezes_before_destructive_creation(install):
    native = "TIMESTAMP(6) WITH TIME ZONE"
    rows = (
        (dt.datetime(2024, 1, 1, 12, 0, 0, 123456, tzinfo=dt.timezone.utc),),
        (dt.datetime(2024, 1, 2, 12, 0, 0, 654321, tzinfo=dt.timezone.utc),),
    )
    state = install("trino", rows=rows, native_type=native)

    result = run("trino", source="SELECT id FROM sandbox.events", drop_target_if_exists=True)

    assert result.rows == 2
    assert state.reads == state.inspections == 1
    assert state.events.index("certified") < state.events.index("drop")
    assert state.inserts == [list(rows)]
    assert state.native_copies == []
    assert state.schema == {"id": native}


def test_trino_calendar_schema_adaptation_certifies_values_before_target_creation(install):
    value = dt.datetime(2024, 1, 1, 12, 0, 0, 123456, tzinfo=dt.timezone.utc)
    state = install("trino", rows=((value,),), native_type="TIMESTAMP(6) WITH TIME ZONE")

    result = run("trino", schema={"id": "TIMESTAMP(3) WITH TIME ZONE"})

    assert result.rows == 1
    assert state.reads == state.inspections == 1
    assert state.events.index("certified") < state.events.index("create")
    assert state.inserts == [[(value.replace(microsecond=123000),)]]
    assert state.native_copies == []
    assert state.schema == {"id": "TIMESTAMP(3) WITH TIME ZONE"}
