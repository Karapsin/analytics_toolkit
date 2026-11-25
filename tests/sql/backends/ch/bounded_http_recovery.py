"""Legacy bounded runners must not confuse distinct native instances over HTTP."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch import lifecycle
from analytics_toolkit.sql.backends.ch.recovery_endpoints import RecoveryEndpoint
from analytics_toolkit.sql.connection.get_sql_connection import get_ch_connection_for_host
from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests.sql._support.ch_drop_scope import install_drop_scope


@pytest.mark.parametrize("driver", ["http", "native"])
def test_bounded_http_runner_retains_ambiguous_same_host_native_instances(
    monkeypatch, write_sql_connections, driver
):
    state = install_drop_scope(monkeypatch, write_sql_connections, driver=driver)
    second = "second_instance"
    state.host_tables[second] = {}
    state.host_commands[second] = []
    state.host_endpoints[second] = (state.remote, "10.0.0.3", 9001)
    state.server_identities[second] = ("00000000-0000-0000-0000-000000000003", 9001)
    pair = lifecycle.ChDistributedTablePair("analytics.events", "analytics.events_shard")
    for table in (pair.distributed_table, pair.shard_table):
        origin = state.coordinator if table == pair.distributed_table else state.remote
        original = state.host_tables[origin][table]
        if table not in state.host_tables[state.remote]:
            state._create(state.remote, original.create.copy(), deepcopy(original.rows))
        state._create(second, original.create.copy(), deepcopy(original.rows))
    open_connection = state.open

    def fixed_http_port(config, **options):
        # Both native-instance names resolve to the configured HTTP service.
        if config.driver == "http" and config.host == state.remote:
            config = replace(config, host=state.host_endpoints[state.remote][1])
        return open_connection(config, **options)

    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", fixed_http_port)
    before = state.snapshot()
    hosts = [RecoveryEndpoint(state.remote, 9000), RecoveryEndpoint(state.remote, 9001)]

    def runner(endpoint, operation):
        connection = get_ch_connection_for_host("ch", endpoint)
        try:
            return operation(connection)
        finally:
            connection.close()

    with backend_operation() as operation:
        if driver == "http":
            with pytest.raises(TimeoutError, match=r"HTTP.*distinguish|cleanup is incomplete"):
                lifecycle._drop_pair_on_bounded_hosts(hosts, pair, None, runner, 2)
            assert operation.preserve_recovery is True
        else:
            lifecycle._drop_pair_on_bounded_hosts(hosts, pair, None, runner, 2)

    if driver == "http":
        assert state.snapshot() == before
        assert state.commands == []
    else:
        expected = deepcopy(before)
        for host in (state.remote, second):
            for table in (pair.distributed_table, pair.shard_table):
                expected[host].pop(table)
        assert state.snapshot() == expected
    assert all(client.close_calls == 1 for client in state.opened)
