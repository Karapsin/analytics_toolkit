"""Internal local SQL keeps explicitly scoped ClickHouse FUNCTION INSERT targets."""

from __future__ import annotations

from contextlib import nullcontext

import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.routing import local_sql
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_transfer_replacement import ReplacementScopeState


@pytest.mark.parametrize("internal_local", [False, True], ids=["automatic", "internal-local"])
def test_public_execute_keeps_explicit_function_insert_on_its_verified_remote_scope(
    monkeypatch, write_sql_connections, internal_local
):
    set_connections_path(
        write_sql_connections(
            {
                "ch": {
                    "type": "ch",
                    "driver": "http",
                    "host": "coordinator",
                    "user": "user",
                    "password": "password",
                    "database": "analytics",
                    "cluster_routing": {"cluster": "core"},
                }
            }
        )
    )
    state = ReplacementScopeState()
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", state.open)
    before = state.snapshot()

    with local_sql() if internal_local else nullcontext():
        sql.execute(
            "ch",
            "INSERT INTO FUNCTION cluster('storage', 'analytics', 'events_shard') SELECT 201",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert state.rows(state.remote, "analytics.events_shard") == [(11,), (12,), (13,), (201,)]
    assert state.snapshot()[state.coordinator] == before[state.coordinator]
    assert len(state.commands) == 1
    inserted = parse_one(strip_query_label(state.commands[0]), read=ClickHouseDialect)
    assert isinstance(inserted, exp.Insert)
    assert inserted.args.get("is_function") is True
    target = inserted.this
    if isinstance(target, exp.Schema):
        target = target.this
    function = target.this if isinstance(target, exp.Table) else target
    assert isinstance(function, exp.Anonymous)
    cluster, database, table, _trailing = cluster_arguments(function, "analytics")
    assert (cluster, database, table) == ("storage", "analytics", "events_shard")
    assert all(connection.closed for connection in state.opened)
