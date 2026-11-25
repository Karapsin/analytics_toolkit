"""Partition deletion mutates only the exact raw ALTER TABLE execution scope."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_lifecycle import LifecycleConnection, install_lifecycle
from tests.sql._support.reconfigure_conversion_scope import _name, _scope_sql


class PartitionConnection(LifecycleConnection):
    def command(self, statement, settings=None):
        cluster, body = _scope_sql(strip_query_label(statement))
        tree = parse_one(body, read=ClickHouseDialect)
        if not isinstance(tree, exp.Alter):
            return super().command(statement, settings=settings)
        assert not self.closed
        partition = tree.find(exp.DropPartition)
        assert partition is not None
        operand = partition.expressions[0].expressions[0]
        assert isinstance(operand, exp.Literal)
        assert operand.is_string
        # The raw query fixture exposes getMacro('cluster') as this same value.
        cluster = "storage" if cluster == "{cluster}" else cluster
        hosts = self.state.clusters[cluster] if cluster is not None else (self.host,)
        name = _name(tree.this)
        self.state.commands.append(statement)
        self.state.command_settings.append(deepcopy(settings))
        for host in hosts:
            self.state.host_commands[host].append(statement)
            table = self.state.host_tables[host][name]
            assert table.route is None
            table.rows[:] = [row for row in table.rows if row[0] != operand.this]
        return None


def install_partition_scope(monkeypatch, write_sql_connections, *, driver, layout):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    state.connection_class = PartitionConnection
    for host in state.host_tables:
        state.host_tables[host].clear()
    physical_name = "external_storage" if layout == "external" else "events_shard"
    physical = parse_create_table(
        f"CREATE TABLE analytics.{physical_name} (p String, id UInt64) "
        "ENGINE=MergeTree PARTITION BY p ORDER BY id",
        f"analytics.{physical_name}",
    )
    state._create(state.remote, physical, [("old", 1), ("kept", 2)])
    foreign = parse_create_table(
        "CREATE TABLE analytics.events_shard (p String, id UInt64) "
        "ENGINE=MergeTree PARTITION BY p ORDER BY id",
        "analytics.events_shard",
    )
    state._create(state.coordinator, foreign, [("old", value) for value in range(100)])
    target = parse_create_table(
        "CREATE TABLE analytics.events (p String, id UInt64) "
        + (
            "ENGINE=MergeTree PARTITION BY p ORDER BY id"
            if layout == "local"
            else f"ENGINE=Distributed('storage','analytics','{physical_name}',rand())"
        ),
        "analytics.events",
    )
    rows = [("old", 1), ("kept", 2)] if layout == "local" else []
    state._create(state.coordinator, target, rows)
    state.created.clear()
    return state
