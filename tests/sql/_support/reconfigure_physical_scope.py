"""Public reconfiguration setup with independent source endpoint storage."""

from __future__ import annotations

from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.sql._support.reconfigure_independent_replicas import (
    IndependentReplicaClient,
    configure_independent_replicas,
    connect_independent_replicas,
)


def install_physical_scope(monkeypatch, write_sql_connections, *, complete=True):
    configure_independent_replicas(write_sql_connections)
    raw = IndependentReplicaClient(internal_replication=True)
    if not complete:
        raw.host_tables[raw.remote].pop(raw.source_physical)
    connect_independent_replicas(monkeypatch, raw)
    return raw


def add_migration_scopes(raw):
    host = "archive"
    raw.clusters["coordinator_only"] = (raw.coordinator,)
    raw.clusters["archive"] = (host,)
    raw.host_tables[host] = {}
    raw.host_commands[host] = []
    raw.host_endpoints[host] = (host, "10.0.0.3", 9000)
    raw.server_identities[host] = ("00000000-0000-0000-0000-000000000003", 9000)


def change_scope_before_execution(monkeypatch, raw):
    adapter = get_backend_adapter("ch")
    execute = adapter.execute_table_reconfiguration
    mutations = []

    def execute_after_scope_change(connection, reconfiguration, *, validate_row_count):
        mutations.append("source_scope_changed")
        raw.clusters["storage"] = (raw.coordinator,)
        return execute(connection, reconfiguration, validate_row_count=validate_row_count)

    monkeypatch.setattr(adapter, "execute_table_reconfiguration", execute_after_scope_change)
    return mutations


def reconfigure_physical(*, order_by="cityHash64(id)", **kwargs):
    return sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        order_by=order_by,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
        **kwargs,
    )
