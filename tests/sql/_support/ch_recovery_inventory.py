"""Raw acknowledgement faults over complete stateful ClickHouse transports."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_lifecycle import LifecycleConnection, install_lifecycle
from tests.sql._support.reconfigure_conversion_scope import _scope_sql


def command_parts(statement):
    scope, body = _scope_sql(strip_query_label(statement))
    return body.lstrip().split(None, 1)[0].upper(), scope, body


class RecoveryConnection(LifecycleConnection):
    def command(self, statement, settings=None):
        self.state.raw_attempts.append((self.host, statement))
        if self.state.before_raw_command is not None:
            self.state.before_raw_command(self, statement)
        result = super().command(statement, settings=settings)
        if self.state.after_raw_command is not None:
            self.state.after_raw_command(self, statement)
        return result


def install_recovery(monkeypatch, write_sql_connections, *, driver):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    state.connection_class = RecoveryConnection
    state.raw_attempts = []
    state.before_raw_command = None
    state.after_raw_command = None
    return state


def created_scopes(state):
    return tuple(
        (name, "storage" if host == state.remote else None)
        for host, name in state.created
        if "__reconfigure_" in name or "__copy_" in name or "__replace_guarded_" in name
    )


def assert_original_resources_retained(state, before):
    """Renames may move resources; every original UUID and its rows must survive."""
    for host, tables in before.items():
        for original in tables.values():
            matches = [
                value
                for value in state.host_tables[host].values()
                if value.uuid == original["uuid"]
            ]
            assert len(matches) == 1
            value = matches[0]
            assert value.rows == original["rows"]
            assert value.route == original["route"]
            old = parse_one(original["ddl"], read=ClickHouseDialect)
            actual = value.create.copy()
            assert isinstance(old, exp.Create)
            actual.this.set("this", old.this.this.copy())
            assert actual == old


def original_snapshots_unchanged(state, before):
    actual = state.snapshot()
    for host, tables in before.items():
        assert {name: actual[host].get(name) for name in tables} == deepcopy(tables)
