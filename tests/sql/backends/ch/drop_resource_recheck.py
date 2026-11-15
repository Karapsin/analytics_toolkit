"""A bounded drop rechecks native table ownership before its first submission."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.drop_bounded import execute_bounded_verified_drop
from analytics_toolkit.sql.backends.ch.drop_execution import DropExecutionOptions
from analytics_toolkit.sql.backends.ch.drop_layout import DropIntent, certify_existing_drop
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.dml.transfer.runtime.connection_pool import BoundedConnectionManager

from tests.sql._support.ch_drop_scope import install_drop_scope

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"


def _change_physical_resource(state, change):
    original = state.host_tables[state.remote][_PHYSICAL]
    if change == "disappear":
        state.host_tables[state.remote].pop(_PHYSICAL)
    elif change == "recreate":
        state.host_tables[state.remote].pop(_PHYSICAL)
        state._create(state.remote, original.create.copy(), deepcopy(original.rows))
    else:
        assert change == "alter-setting"
        original.create = parse_create_table(
            f"CREATE TABLE {_PHYSICAL} (id UInt64) ENGINE=MergeTree ORDER BY id "
            "SETTINGS parts_to_throw_insert=6000",
            _PHYSICAL,
        )


@pytest.mark.parametrize("change", ["disappear", "recreate", "alter-setting"])
def test_drop_resource_recheck_rejects_external_change_before_any_ddl_with_capacity_released(
    monkeypatch, write_sql_connections, change
):
    state = install_drop_scope(monkeypatch, write_sql_connections, driver="native")
    before = state.snapshot()
    faults = []
    roles = []
    manager = BoundedConnectionManager(
        "ch", 1, role="bounded native ownership recheck", open_connection=sql.get_sql_connection
    )

    def concurrent_change(_connection, _statement):
        if faults:
            return
        _change_physical_resource(state, change)
        faults.append(state.snapshot())

    def certify(connection):
        certificate = certify_existing_drop(connection, _TARGET, DropIntent("ch", "storage"))
        assert [resource.role for resource in certificate.resources] == ["facade", "physical"]
        state.before_query = concurrent_change
        return certificate

    def run(role, operation):
        roles.append(role)
        return manager.run(role, lambda ref: operation(ref["connection"]))

    try:
        with pytest.raises(InvalidSqlInputError, match="changed after drop ownership was verified"):
            execute_bounded_verified_drop(
                get_backend_adapter("ch"), run, certify, DropExecutionOptions(wait_for_absence=True)
            )
        assert manager._available.qsize() == 1
        assert manager.run("reuse capacity after failed ownership recheck", lambda _ref: True)
        assert manager.high_water_mark == 1
    finally:
        manager.close()

    assert faults == [state.snapshot()]
    assert roles == ["certify_target_drop", "preflight_target_drop"]
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())
    assert state.snapshot()[state.coordinator] == before[state.coordinator]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    if change == "recreate":
        replacement = state.snapshot()[state.remote][_PHYSICAL]
        original = before[state.remote][_PHYSICAL]
        assert replacement["uuid"] != original["uuid"]
        assert replacement["ddl"] == original["ddl"]
        assert replacement["rows"] == original["rows"]
    elif change == "alter-setting":
        replacement = state.snapshot()[state.remote][_PHYSICAL]
        original = before[state.remote][_PHYSICAL]
        assert replacement["uuid"] == original["uuid"]
        assert replacement["ddl"] != original["ddl"]
        assert replacement["rows"] == original["rows"]
    else:
        assert _PHYSICAL not in state.host_tables[state.remote]
    assert {config.host for config in state.opened_configs} == {state.coordinator}
    assert state.opened
    assert all(connection.closed and connection.close_calls == 1 for connection in state.opened)
