"""Owned cleanup uses submitted UUIDs and the actual CREATE execution scope."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.created_resources import (
    CreatedIdentity,
    drop_created_resource,
)
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.ddl.creation_resources import CreatedResourceChangedError
from analytics_toolkit.sql.ddl.ownership import execute_owned_create_sqls
from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests.sql._support.ch_lifecycle import install_lifecycle


@pytest.fixture
def owned_environment(monkeypatch, write_sql_connections, driver):
    with backend_operation() as operation:
        yield install_lifecycle(monkeypatch, write_sql_connections, driver=driver), operation


def _drop_twice(adapter, connection, receipt, state, original):
    drop_created_resource(adapter, connection, receipt)
    assert state.snapshot() == original
    commands = list(state.commands)
    drop_created_resource(adapter, connection, receipt)
    assert state.commands == commands
    assert state.snapshot() == original


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize(
    "cleanup_scope",
    [(None, False), (None, True), ("storage", False), ("storage", True)],
    ids=["local-complete", "local-partial", "remote-complete", "remote-partial"],
)
@pytest.mark.parametrize("recreated", [False, True], ids=["owned", "new-generation"])
def test_submitted_create_generation_controls_exact_scoped_cleanup(
    owned_environment, cleanup_scope, recreated
):
    scope, partial = cleanup_scope
    state, operation = owned_environment
    adapter = get_backend_adapter("ch")
    target = "analytics.owned_stage"
    host = state.remote if scope else state.coordinator
    foreign_host = state.coordinator if scope else state.remote
    definition = parse_create_table(f"CREATE TABLE {target} (id UInt64) ENGINE=Memory", target)
    state._create(foreign_host, definition, [(value,) for value in range(100)])
    original = state.snapshot()
    state.created.clear()
    resource_names = []
    receipts = []
    connection = state.open(get_connection_config("ch"))
    try:
        cluster = " ON CLUSTER storage" if scope else ""
        execute_owned_create_sqls(
            adapter,
            connection,
            [f"CREATE TABLE {target}{cluster} (id UInt64) ENGINE=Memory"],
            resource_names.append,
            on_scoped_resource_created=receipts.append,
            connection_key="ch",
        )

        assert len(resource_names) == len(receipts) == 1
        receipt = receipts[0]
        assert receipt.scope.kind == ("cluster" if scope else "local")
        assert receipt.scope.cluster == scope
        assert isinstance(receipt.certificate, CreatedIdentity)
        created = state.host_tables[host][target]
        assert created.uuid == receipt.certificate.uuid
        assert created.uuid != state.host_tables[foreign_host][target].uuid
        state.insert_rows(host, target, [(9,)])
        assert operation.resources.get(("ch_owned", "ch", ("analytics", "owned_stage")))
        operation.observed_partial = partial
        if recreated:
            state.host_tables[host].pop(target)
            state._create(host, definition, [(777,)])
            replacement = state.snapshot()
            commands = list(state.commands)

            with pytest.raises(CreatedResourceChangedError, match=r"changed|[Rr]etain"):
                drop_created_resource(adapter, connection, receipt)

            assert state.snapshot() == replacement
            assert state.commands == commands
        else:
            _drop_twice(adapter, connection, receipt, state, original)
        assert operation.partial is partial
    finally:
        connection.close()

    assert state.opened
    assert all(raw.close_calls == 1 for raw in state.opened)
