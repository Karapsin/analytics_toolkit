"""Strict source ownership requires physical presence on every execution host."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import (
    ClickHouseClusterTopologyError,
    InvalidSqlInputError,
)
from analytics_toolkit.sql.execution.observation import observe_sql
from analytics_toolkit.sql.execution.plans import SqlPlan

from tests.sql._support.reconfigure_pair_scope import (
    configure_pair_scope,
    connect_pair_scope,
)
from tests.sql._support.reconfigure_source_presence import MetadataObserver, SourcePresenceClient


def _structural_plan():
    return sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        order_by="id % 2",
        dry_run=True,
        retry_cnt=1,
        timeout_increment=0,
    )


def _assert_actual_transport(raw, observer, original, *, positions):
    assert len(raw.wrappers) == 1
    wrapper = raw.wrappers[0]
    assert isinstance(wrapper, OutageClient)
    assert wrapper._client._sql_observed is True
    assert observer.statements
    assert (raw.clusters["storage"], (1, 2)) in raw.reachability
    assert (raw.clusters["storage"], positions) in raw.physical_presence
    assert raw.commands == []
    assert raw.snapshot() == original
    assert raw.closed


def test_reconfigure_rejects_missing_physical_replica_before_strict_source_plan(
    monkeypatch, write_sql_connections
):
    configure_pair_scope(write_sql_connections, None)
    assert get_connection_config("ch").cluster_routing is None
    raw = SourcePresenceClient(complete=False)
    original = raw.snapshot()
    connect_pair_scope(monkeypatch, raw)
    observer = MetadataObserver()

    with observe_sql(observer):
        try:
            with pytest.raises(
                (InvalidSqlInputError, ClickHouseClusterTopologyError),
                match=r"(?i)(complete|missing|presence|coverage|physical|source)",
            ):
                _structural_plan()
        finally:
            _assert_actual_transport(raw, observer, original, positions=(1,))


def test_reconfigure_complete_physical_presence_allows_strict_source_plan(
    monkeypatch, write_sql_connections
):
    configure_pair_scope(write_sql_connections, None)
    assert get_connection_config("ch").cluster_routing is None
    raw = SourcePresenceClient(complete=True)
    original = raw.snapshot()
    connect_pair_scope(monkeypatch, raw)
    observer = MetadataObserver()

    with observe_sql(observer):
        plan = _structural_plan()

    assert isinstance(plan, SqlPlan)
    assert plan.options["strategy"] == "managed_pair_rebuild"
    assert plan.options["order_by"] == "id % 2"
    _assert_actual_transport(raw, observer, original, positions=(1, 2))
