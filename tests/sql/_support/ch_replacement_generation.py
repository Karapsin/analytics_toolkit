"""External DROP/CREATE during real off-side loading preserves part counters."""

from __future__ import annotations

from analytics_toolkit.sql.backends.ch.reconfigure_ddl import retarget_create

from tests.sql._support.ch_lifecycle import LifecycleConnection, install_lifecycle


class RecreatedTargetConnection(LifecycleConnection):
    def _insert(self, target, rows, columns=None):
        super()._insert(target, rows, columns)
        hosts, name = self._reference(target)
        if self.state.recreated or "events_shard__replace_guarded_" not in name:
            return
        self.state.recreated = True
        host = (
            self.state.remote if self.state.changed_role == "physical" else self.state.coordinator
        )
        target_name = (
            "analytics.events_shard"
            if self.state.changed_role == "physical"
            else "analytics.events"
        )
        previous = self.state.host_tables[host].pop(target_name)
        definition = retarget_create(previous.create, target_name, None)
        values = [(111,), (112,), (113,)] if previous.route is None else []
        self.state._create(host, definition, values)
        self.state.changed_snapshot = self.state.snapshot()
        self.state.offside = hosts[0], name


def install_recreated_target(monkeypatch, write_sql_connections, *, driver, role):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    state.connection_class = RecreatedTargetConnection
    state.changed_role = role
    state.recreated = False
    state.changed_snapshot = None
    state.offside = None
    return state
