"""A fresh-target transfer never acquires a competing table through IF NOT EXISTS."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.dml.transfer.flow import finalize
from psycopg2 import extensions
from sqlglot import exp

from tests._support.clock import FakeClock
from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.ch_transfer_replacement import ReplacementScopeState
from tests.sql._support.outage_gp import Connection, Cursor, MemoryGreenplum


class TargetAlreadyExistsError(RuntimeError):
    code = 57
    analytics_toolkit_sql_retry_safe = False


@pytest.mark.parametrize("transport", ["native", "http"])
def test_public_transfer_fresh_target_collision_keeps_competing_hundred_rows(
    monkeypatch, write_sql_connections, transport
):
    monkeypatch.setattr(finalize, "time", FakeClock())
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["ch"].update(
        driver=transport,
        port=9000 if transport == "native" else 8123,
        host="coordinator",
        database="analytics",
        transfer_staging_schema="analytics",
        ddl_defaults={
            "regular": {
                "create_distributed_pair": False,
                "shard": {"engine": "MergeTree", "on_cluster": None},
            },
            "staging": {
                "create_distributed_pair": False,
                "shard": {"engine": "Memory", "on_cluster": None},
            },
        },
    )
    write_sql_connections(configs)
    source = MemoryGreenplum()
    source.tables["db.source"] = pd.DataFrame({"id": [201, 202]})
    state = ReplacementScopeState()
    state.host_tables[state.coordinator].pop("analytics.events")
    state.host_tables[state.remote].pop("analytics.events_shard")
    original_create = state._create
    original_select = source.select
    competitor = []

    def refuse_colliding_plain_create(host, tree, rows=()):
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        name = f"{target.db}.{target.name}"
        if name in state.host_tables[host]:
            message = f"Table {name} already exists"
            raise TargetAlreadyExistsError(message)
        return original_create(host, tree, rows)

    def compete_during_selected_source_read(statement):
        frame = original_select(statement)
        probe = any(
            node.alias == "source_schema_probe" for node in statement.find_all(exp.Subquery)
        )
        if not competitor and not probe and statement.find(exp.Count) is None:
            table = parse_create_table(
                "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY id",
                "analytics.events",
            )
            original_create(state.coordinator, table, [(value,) for value in range(100)])
            competitor.append(state.snapshot())
        return frame

    def register_type(_caster, cursor):
        assert isinstance(cursor, Cursor)

    monkeypatch.setattr(extensions, "register_type", register_type)
    monkeypatch.setattr(source, "select", compete_during_selected_source_read)
    monkeypatch.setattr(state, "_create", refuse_colliding_plain_create)
    monkeypatch.setattr(
        get_backend_adapter("gp"), "open_connection", lambda *_a, **_kw: Connection(source)
    )
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", state.open)

    with pytest.raises(Exception, match=r"already exists|row[- ]count|ownership|acknowledged"):
        sql.transfer(
            "gp",
            "ch",
            from_sql="SELECT id FROM db.source",
            to_table="analytics.events",
            table_schema={"id": "UInt64"},
            write_mode="replace",
            ignore_source_staging=True,
            validate_row_count=True,
            retry_cnt=1,
            full_retry_cnt=1,
            timeout_increment=0,
            full_timeout_increment=0,
            ch_ddl_ready_timeout_seconds=0.01,
        )

    assert len(competitor) == 1
    assert state.snapshot() == competitor[0]
    assert state.rows(state.coordinator, "analytics.events") == [(value,) for value in range(100)]
    assert state.rows(state.coordinator, "analytics.events_shard") == state.foreign_rows
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == [(201,), (202,)]
    assert state.opened
    assert all(connection.closed for connection in state.opened)
