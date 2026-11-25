"""Recovery must address the actual native instance and report HTTP ambiguity."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.backends.ch import lifecycle
from analytics_toolkit.sql.backends.ch.outage_hosts import host_connection
from analytics_toolkit.sql.backends.ch.outage_topology import Host
from analytics_toolkit.sql.backends.ch.recovery_endpoints import RecoveryEndpoint
from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError
from analytics_toolkit.sql.connection.get_sql_connection import get_ch_connection_for_host
from analytics_toolkit.sql.execution.backend_options import BackendOperationState, backend_operation


def test_discovered_recovery_tcp_ports_reach_native_factory(outage: Any) -> None:
    cluster, configure = outage
    configure(driver="native")
    endpoint = RecoveryEndpoint("h1", 9011)
    connection = get_ch_connection_for_host("ch", endpoint)
    connection.close()
    assert cluster.connections[-1] == ("h1", 9011)
    configure(driver="http", port=8125)
    connection = get_ch_connection_for_host("ch", endpoint)
    connection.close()
    assert cluster.connections[-1] == ("h1", 8125)


def test_configured_recovery_membership_keeps_multiple_ports_on_one_hostname(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        lifecycle, "_resolve_ch_cluster_name_for_wait", lambda _connection, cluster: cluster
    )
    connection = SimpleNamespace(
        query=lambda _sql: SimpleNamespace(result_rows=[("same", 9000), ("same", 9001)])
    )
    hosts = lifecycle._query_ch_configured_cluster_hosts(connection, "core")
    assert [host.tcp_port for host in hosts] == [9000, 9001]
    assert [host.identity for host in hosts] == ["same:9000", "same:9001"]


@pytest.mark.parametrize("tracked", [False, True])
def test_http_recovery_retains_artifacts_when_instances_share_hostname(
    monkeypatch: Any, tracked: bool
) -> None:
    hosts = [RecoveryEndpoint("same", 9000), RecoveryEndpoint("same", 9001)]
    monkeypatch.setattr(lifecycle, "_query_ch_configured_cluster_hosts", lambda *_: hosts)
    monkeypatch.setattr(
        lifecycle, "_select_ch_hosts_for_local_drop", lambda *_args, **_kwargs: hosts
    )
    calls = []
    context = backend_operation() if tracked else nullcontext(None)
    with context as state:
        with pytest.raises(TimeoutError, match="artifacts are retained"):
            lifecycle._drop_ch_distributed_table_pair_on_cluster_hosts(
                SimpleNamespace(is_native_transport=False),
                lifecycle.ch_distributed_table_pair("db.events"),
                ch_cluster="core",
                query_label=None,
                per_host_drop_workers=2,
                per_host_connection_factory=calls.append,
            )
        if state is not None:
            assert state.preserve_recovery is True
    assert calls == []


def test_direct_http_host_control_refuses_ambiguous_instances_before_connection() -> None:
    hosts = [Host(1, 1, 1, "same", 9000), Host(2, 2, 1, "same", 9001)]
    state = BackendOperationState()
    scope = SimpleNamespace(config=SimpleNamespace(driver="http"), hosts=hosts, state=state)
    with pytest.raises(ClickHouseClusterTopologyError, match="cannot distinguish"), host_connection(
        scope, hosts[0]
    ):
        pytest.fail("ambiguous host must not be contacted")
    assert state.preserve_recovery is True
