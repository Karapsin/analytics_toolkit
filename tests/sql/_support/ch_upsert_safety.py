"""Endpoint SQL state for public ClickHouse partition-rebuild safety tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime

from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_lifecycle import LifecycleConnection, install_lifecycle
from tests.sql._support.ch_transfer_replacement import _parse_command, _Result
from tests.sql._support.reconfigure_conversion_scope import _name, _scope_sql
from tests.sql._support.upsert_scope import StatefulUpsertClient
from tests.sql.backends.ch._support.exact_transport import native_context

TARGET = "analytics.events"
PHYSICAL = "analytics.events_shard"
COLUMNS = ("month", "id", "value")
ORIGINAL = [(1, 1, 10), (1, 2, 20), (2, 3, 30)]
INCOMING = [(1, 1, 11)]
EXPECTED = [(1, 1, 11), (1, 2, 20), (2, 3, 30)]
DATE_COLUMNS = ("date", "id", "value")
DATE_ORIGINAL = [(date(2024, 1, 1), 1, 10), (date(2024, 1, 2), 2, 20), (date(2024, 2, 1), 3, 30)]
DATE_INCOMING = [(date(2024, 1, 1), 1, 11)]
DATE_EXPECTED = [(date(2024, 1, 1), 1, 11), (date(2024, 1, 2), 2, 20), (date(2024, 2, 1), 3, 30)]


@dataclass(frozen=True)
class UpsertScenario:
    driver: str
    local: bool = False
    partition: str = "month"
    replicas: bool = False
    replicated: bool = False
    external_columns: bool = False
    retarget: bool = False
    replication_lag: bool = True
    date_partition: bool = False
    wide_value: bool = False


def _integer_cast(value, native):
    lowered = native.lower()
    if lowered.startswith("uint") and lowered[4:].isdigit():
        return int(value) % (1 << int(lowered[4:]))
    if lowered.startswith("int") and lowered[3:].isdigit():
        bits = int(lowered[3:])
        encoded = int(value) % (1 << bits)
        return encoded - (1 << bits) if encoded >= 1 << (bits - 1) else encoded
    return value


def _selected_hosts(state, cluster):
    """Native Distributed/cluster reads select one available replica per shard."""
    selected = {}
    for host in state.clusters[cluster]:
        shard, _replica = state.replica_positions.get((cluster, host), (1, 1))
        selected.setdefault(shard, host)
    return tuple(selected.values())


def _read_visible(state, host, name):
    value = state.host_tables[host][name]
    if value.route is None:
        return deepcopy(value.rows)
    cluster, physical = value.route
    rows = []
    for endpoint in _selected_hosts(state, cluster):
        source = state.host_tables[endpoint][physical]
        rows.extend(
            tuple(dict(zip(source.schema, row))[column] for column in value.schema)
            for row in _read_visible(state, endpoint, physical)
        )
    return rows


class _EndpointRows(StatefulUpsertClient):
    """Use the common JOIN/HAVING evaluator over actual endpoint table objects."""

    def __init__(self, connection):
        self.connection = connection

    def _source_data(self, source):
        if isinstance(source, exp.Subquery):
            return self._select_data(source.this)
        hosts, name = self.connection._reference(source)
        names = list(self.connection.state.host_tables[hosts[0]][name].schema)
        rows = [row for host in hosts for row in _read_visible(self.connection.state, host, name)]
        return rows, names

    def _value(self, node, row):
        if isinstance(node, exp.Cast):
            value = self._value(node.this, row)
            native = node.args["to"].sql(dialect=ClickHouseDialect)
            if native in {"Date", "Date32"}:
                return (
                    value.date() if isinstance(value, datetime) else date.fromisoformat(str(value))
                )
            if native.lower().startswith(("int", "uint")):
                return _integer_cast(value, native)
            return value
        if isinstance(node, exp.Mod):
            return self._value(node.this, row) % self._value(node.expression, row)
        if isinstance(node, exp.Anonymous) and node.name.casefold() == "toyyyymm":
            value = self._value(node.expressions[0], row)
            assert isinstance(value, date), "toYYYYMM requires the target-cast native date."
            return value.year * 100 + value.month
        return super()._value(node, row)


def _partition_value(expression, row):
    if isinstance(expression, exp.Column):
        return row[expression.name]
    if isinstance(expression, exp.Literal):
        return expression.to_py()
    if isinstance(expression, (exp.Paren, exp.PartitionedByProperty)):
        return _partition_value(expression.this, row)
    return _partition_composite(expression, row)


def _partition_composite(expression, row):
    if isinstance(expression, exp.Mod):
        return _partition_value(expression.this, row) % _partition_value(expression.expression, row)
    if isinstance(expression, exp.Tuple):
        return tuple(_partition_value(item, row) for item in expression.expressions)
    if isinstance(expression, exp.Anonymous):
        return _partition_function(expression, row)
    message = f"Unsupported fake physical partition expression: {expression}"
    raise AssertionError(message)


def _partition_function(expression, row):
    if expression.name.casefold() in {"todate", "todate32"}:
        value = _partition_value(expression.expressions[0], row)
        return value.date() if isinstance(value, datetime) else date.fromisoformat(str(value))
    if expression.name.casefold() == "toyyyymm":
        value = _partition_value(expression.expressions[0], row)
        assert isinstance(value, date)
        return value.year * 100 + value.month
    message = f"Unsupported fake physical partition expression: {expression}"
    raise AssertionError(message)


class UpsertSafetyConnection(LifecycleConnection):
    def __init__(self, state, host, *, native):
        super().__init__(state, host, native=native)
        if native:
            self.context = native_context()

    def _reference(self, target):
        hosts, name = super()._reference(target)
        source = target.this if isinstance(target, exp.Schema) else target
        function = source.this if isinstance(source, exp.Table) else source
        if isinstance(function, exp.Anonymous) and function.name.casefold() == "cluster":
            cluster, _database, _table, _trailing = cluster_arguments(function, "analytics")
            hosts = _selected_hosts(self.state, cluster)
        return hosts, name

    def _metadata(self, name, hosts):
        if name == "system.replicas":
            return [
                {
                    **self.state._host_record(host),
                    "_shard_num": index,
                    "database": table.split(".", 1)[0],
                    "table": table.split(".", 1)[1],
                    "zookeeper_path": "/clickhouse/tables/analytics/events_shard/1",
                    "zookeeper_name": "default",
                    "replica_name": host,
                    "is_readonly": 0,
                    "is_session_expired": 0,
                    "queue_size": len(self.state.pending_replication.get((host, table), [])),
                    "inserts_in_queue": len(self.state.pending_replication.get((host, table), [])),
                    "absolute_delay": int(bool(self.state.pending_replication.get((host, table)))),
                    "log_max_index": 2,
                    "log_pointer": 2 if self.state.pending_replication.get((host, table)) else 3,
                    "total_replicas": 2,
                    "active_replicas": 2,
                }
                for index, host in enumerate(hosts, 1)
                for table, value in self.state.host_tables[host].items()
                if value.engine.lower().startswith("replicated")
            ]
        if name == "system.zookeeper":
            return [
                {
                    "path": f"/clickhouse/tables/analytics/events_shard/1/replicas/{host}",
                    "name": "is_active",
                    "zookeeperName": "default",
                    "value": f"UUID_'{self.state.server_identities[host][0]}'",
                    "ephemeralOwner": index + 1,
                }
                for index, host in enumerate(self.state.clusters["storage"])
            ]
        records = super()._metadata(name, hosts)
        if name == "system.clusters":
            for record in records:
                endpoint = next(
                    host
                    for host, address in self.state.host_endpoints.items()
                    if address[0] == record["host_name"]
                )
                position = self.state.replica_positions.get((record["cluster"], endpoint))
                if position is not None:
                    record["shard_num"], record["replica_num"] = position
                record["internal_replication"] = int(self.state.replicated)
        return records

    def _select(self, select):
        sources = list(select.find_all(exp.Table))
        if not sources or any(
            self._reference(source)[1].startswith("system.") for source in sources
        ):
            return super()._select(select)
        rows, names = _EndpointRows(self)._select_data(select)
        return _Result(rows, names)

    def _write(self, host, name, rows, columns):
        value = self.state.host_tables[host][name]
        if value.route is not None:
            cluster, physical = value.route
            # All cases use one shard. Independent replicas receive separate
            # blocks; replicated storage exposes the eventual replicated result.
            for endpoint in self.state.clusters[cluster]:
                self._write(endpoint, physical, rows, columns)
            return
        assert set(columns) <= set(value.schema)
        value.rows.extend(
            tuple(
                _integer_cast(
                    dict(zip(columns, row)).get(column, "" if "String" in native else 0), native
                )
                for column, native in value.schema.items()
            )
            for row in rows
        )

    def _insert(self, target, rows, columns=None):
        rows = [tuple(row) for row in rows]
        hosts, name = self._reference(target)
        if isinstance(target, exp.Schema):
            columns = [item.name for item in target.expressions]
        columns = columns or list(self.state.host_tables[hosts[0]][name].schema)
        for host in hosts:
            self._write(host, name, rows, columns)

    def command(self, statement, settings=None):
        cluster, body = _scope_sql(strip_query_label(statement))
        prefix = "SYSTEM SYNC REPLICA "
        if body.strip().upper().startswith(prefix):
            hosts = self.state.clusters[cluster] if cluster is not None else (self.host,)
            name = _name(body.strip()[len(prefix) :].strip())
            self.state.commands.append(statement)
            self.state.command_settings.append(deepcopy(settings))
            for host in hosts:
                value = self.state.host_tables[host][name]
                assert value.engine.lower().startswith("replicated")
                self.state.host_commands[host].append(statement)
                pending = self.state.pending_replication.pop((host, name), [])
                value.rows.extend(deepcopy(pending))
                self.state.sync_events.append((host, name, len(self.state.commands)))
            return None
        tree = _parse_command(body)
        if isinstance(tree, exp.Alter):
            assert all(isinstance(item, exp.DropPartition) for item in tree.args["actions"])
            self.state.commands.append(statement)
            self.state.command_settings.append(deepcopy(settings))
            hosts = self.state.clusters[cluster] if cluster is not None else (self.host,)
            name = _name(tree.this)
            for host in hosts:
                self.state.host_commands[host].append(statement)
                value = self.state.host_tables[host][name]
                partition = value.create.find(exp.PartitionedByProperty)
                for action in tree.args["actions"]:
                    selected = _partition_value(action.expressions[0].expressions[0], {})
                    value.rows[:] = [
                        row
                        for row in value.rows
                        if _partition_value(partition, dict(zip(value.schema, row))) != selected
                    ]
            return None
        result = super().command(statement, settings=settings)
        if isinstance(tree, exp.Insert):
            self._after_payload_insert(tree)
        return result

    def _after_payload_insert(self, tree):
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        name = _name(target)
        preservation = any(
            table.alias == "target_dst" for table in tree.expression.find_all(exp.Table)
        )
        if preservation:
            self.state.preserved_payload = name
            self.state.preservation_events.append(len(self.state.commands))
        elif self.state.retarget and name == self.state.preserved_payload:
            self.state.retarget = False
            self.state.retargeted = True
            value = self.state.host_tables[self.state.coordinator][TARGET]
            value.create = parse_one(
                f"CREATE TABLE {TARGET} (month Int64, id Int64, value Int64) "
                "ENGINE=Distributed('replacement', 'analytics', 'events_shard', rand())",
                read=ClickHouseDialect,
            )
            value.route = "replacement", PHYSICAL
            self.state.changed_snapshot = self.state.snapshot()


def _install_peer_endpoints(state, scenario):
    if scenario.replicas or scenario.retarget:
        state.host_tables["replica"] = {}
        state.host_commands["replica"] = []
        state.host_endpoints["replica"] = ("replica", "10.0.0.3", 9001)
        state.server_identities["replica"] = ("00000000-0000-0000-0000-000000000003", 9001)
    if scenario.replicas:
        state.clusters["storage"] = (state.remote, "replica")
        state.replica_positions.update(
            {("storage", state.remote): (1, 1), ("storage", "replica"): (1, 2)}
        )
    if scenario.retarget:
        state.clusters["replacement"] = ("replica",)


def _seed_peer_storage(state, scenario, physical, create):
    if scenario.replicas:
        first = [(1, 1, 10), (2, 3, 30)] if scenario.replicated else [(1, 1, 10)]
        state.host_tables[state.remote][physical].rows = (
            list(ORIGINAL) if scenario.replicated and not scenario.replication_lag else first
        )
        state._create("replica", create, ORIGINAL if scenario.replicated else [(1, 2, 20)])
        if scenario.replicated and scenario.replication_lag:
            state.pending_replication[state.remote, physical] = [(1, 2, 20)]
    if scenario.retarget:
        state._create("replica", create, [(1, 1, 900), (1, 4, 400), (2, 5, 500)])


def install_upsert_safety(monkeypatch, write_sql_connections, *, scenario: UpsertScenario):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=scenario.driver)
    state.connection_class = UpsertSafetyConnection
    state.replica_positions = {}
    state.replicated = scenario.replicated
    state.retarget = scenario.retarget
    state.retargeted = False
    state.preserved_payload = None
    state.changed_snapshot = None
    state.pending_replication = {}
    state.sync_events = []
    state.preservation_events = []
    for tables in state.host_tables.values():
        tables.clear()
    _install_peer_endpoints(state, scenario)
    physical = (
        TARGET
        if scenario.local
        else "analytics.external_storage"
        if scenario.external_columns
        else PHYSICAL
    )
    fields = (
        "date Date, id Int64, value Int64"
        if scenario.date_partition
        else "month Int64, id Int64, value Int64"
        + (", note String" if scenario.external_columns else "")
    )
    if scenario.wide_value:
        fields = fields.replace("value Int64", "value UInt64")
    engine = (
        "ReplicatedMergeTree('/clickhouse/tables/analytics/events_shard/1', '{replica}')"
        if scenario.replicated
        else "MergeTree"
    )
    create = parse_one(
        f"CREATE TABLE {physical} ({fields}) ENGINE={engine} "
        f"PARTITION BY {scenario.partition} ORDER BY id",
        read=ClickHouseDialect,
    )
    rows = (
        DATE_ORIGINAL
        if scenario.date_partition
        else ([(1, 1, 10), (11, 2, 20), (2, 3, 30)] if scenario.partition != "month" else ORIGINAL)
    )
    if scenario.external_columns:
        rows = [(*row, f"private-{row[1]}") for row in rows]
    if scenario.wide_value:
        rows = [
            (month, identifier, 1000 if identifier == 2 else value)
            for month, identifier, value in rows
        ]
    state._create(state.coordinator if scenario.local else state.remote, create, rows)
    if not scenario.local:
        value_type = "UInt64" if scenario.wide_value else "Int64"
        facade = parse_one(
            f"CREATE TABLE {TARGET} (month Int64, id Int64, value {value_type}) "
            f"ENGINE=Distributed('storage','analytics','{physical.split('.')[1]}',rand())",
            read=ClickHouseDialect,
        )
        state._create(state.coordinator, facade)
    _seed_peer_storage(state, scenario, physical, create)
    state.created.clear()
    return state
