"""Typed keyed data behind the real outage adapter and snapshot SQL paths."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.outage_context import INTERNAL_IO
from sqlglot import exp, parse_one

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.outage import Client, Cluster, Result
from tests.sql._support.outage_gp import MemoryGreenplum


def _value(expression: Any, record: dict[str, Any], ordinal: int, count: int) -> Any:
    if isinstance(expression, (exp.Alias, exp.Cast, exp.Paren)):
        return _value(expression.this, record, ordinal, count)
    if isinstance(expression, exp.Column):
        return record[expression.name]
    if isinstance(expression, exp.Literal):
        return expression.to_py()
    if isinstance(expression, exp.Window) and isinstance(expression.this, exp.RowNumber):
        return ordinal
    if isinstance(expression, exp.Count):
        return count
    if isinstance(expression, (exp.And, exp.EQ, exp.GTE, exp.LT)):
        return _boolean_value(expression, record, ordinal, count)
    message = f"Unsupported snapshot data expression: {expression}"
    raise AssertionError(message)


def _boolean_value(expression: Any, record: dict[str, Any], ordinal: int, count: int) -> Any:
    left = _value(expression.this, record, ordinal, count)
    if isinstance(expression, exp.And):
        return left and _value(expression.expression, record, ordinal, count)
    right = _value(expression.expression, record, ordinal, count)
    if isinstance(expression, exp.EQ):
        return left == right
    return left >= right if isinstance(expression, exp.GTE) else left < right


def _native_type(expression: Any, types: dict[str, str]) -> str:
    if isinstance(expression, exp.Alias):
        return _native_type(expression.this, types)
    if isinstance(expression, exp.Cast):
        return expression.args["to"].sql(dialect=ClickHouseDialect)
    return types[expression.name] if isinstance(expression, exp.Column) else "UInt64"


class SnapshotScopeCluster(Cluster):
    def __init__(self, *, change_scope: bool) -> None:
        super().__init__()
        self.change_scope = change_scope
        self.change_pending = False
        self.coverage_changes: list[Any] = []
        self.populations: list[tuple[str, list[tuple[Any, ...]]]] = []
        self.clients: list[SnapshotScopeClient] = []
        self.seed()
        for host in self.hosts:
            for table in ("db.events", "db.events_shard"):
                self.tables.pop((host, table))
            self.create(
                host,
                "CREATE TABLE db.events_shard (id Int256) ENGINE=MergeTree ORDER BY id",
            )
            self.create(
                host,
                "CREATE TABLE db.events (id Int256) "
                "ENGINE=Distributed('core', 'db', 'events_shard', rand())",
            )
            self.tables[(host, "db.events_shard")].rows = [(1,), (2,)]

    def open(self, config: Any, **_kwargs: Any) -> Any:
        self.connections.append((config.host, config.port))
        assert config.host in self.alive
        raw = SnapshotScopeClient(self, config.host)
        self.clients.append(raw)
        return raw if INTERNAL_IO.get() else OutageClient(raw, config)

    def observe_source_metadata(self, statement: Any) -> None:
        if (
            not self.change_pending
            or self.coverage_changes
            or not isinstance(statement, exp.Select)
        ):
            return
        source = statement.args.get("from_")
        table = source.this if source is not None else None
        if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Anonymous):
            return
        if table.this.name.casefold() not in {"cluster", "clusterallreplicas"}:
            return
        cluster, database, relation, _trailing = cluster_arguments(table.this, "db")
        condition = statement.args.get("where")
        filters = (
            {
                item.this.name: item.expression.this
                for item in condition.find_all(exp.EQ)
                if isinstance(item.this, exp.Column)
                and isinstance(item.expression, exp.Literal)
                and item.expression.is_string
            }
            if condition is not None
            else {}
        )
        if (cluster, database, relation, filters) != (
            "core",
            "system",
            "tables",
            {"database": "db", "name": "events_shard"},
        ):
            return
        completed = [row for _table, rows in self.populations for row in rows]
        assert completed
        assert {row[1] for row in completed} == {0}
        self.coverage_changes.append(deepcopy(completed))
        self.tables.pop(("h2", "db.events_shard"))


class SnapshotScopeClient(Client):
    def query(self, sql: str, **kwargs: Any) -> Result:
        statement = parse_one(sql, read=ClickHouseDialect)
        self.cluster.observe_source_metadata(statement)
        if isinstance(statement, exp.Select) and self._data_select(statement):
            self._validate_settings(kwargs.get("settings") or {})
            self.cluster.queries.append(sql)
            return self._select(statement, kwargs.get("settings") or {})
        return super().query(sql, **kwargs)

    def _data_select(self, statement: Any) -> bool:
        source = statement.args.get("from_")
        table = source.this if source is not None else None
        if isinstance(table, exp.Subquery):
            return self._data_select(table.this)
        if not isinstance(table, exp.Table):
            return False
        if isinstance(table.this, exp.Anonymous):
            if table.this.name.casefold() not in {"cluster", "clusterallreplicas"}:
                return False
            return cluster_arguments(table.this, "db")[1] != "system"
        return table.db != "system"

    def _source_data(self, table: Any, settings: Any) -> Result:
        if isinstance(table, exp.Subquery):
            return self._select(table.this, settings)
        function = table.this
        if isinstance(function, exp.Identifier):
            name = f"{table.db or 'db'}.{table.name}"
            obj = self.cluster.tables[(self.host, name)]
            if obj.engine == "Distributed":
                function = parse_one(obj.engine_full, read=ClickHouseDialect)
                function = exp.Anonymous(this="cluster", expressions=function.expressions)
            else:
                hosts = [self.host]
        if isinstance(function, exp.Anonymous):
            cluster, database, relation, _trailing = cluster_arguments(function, "db")
            name = f"{database}.{relation}"
            members = self.cluster.members_by_cluster.get(cluster, self.cluster.members)
            hosts = self._select_hosts(function, members, name, settings)
        self.cluster.selected_reads.extend((host, name) for host in hosts)
        objects = [self.cluster.tables[(host, name)] for host in hosts]
        columns = list(parse_one(objects[0].ddl, read=ClickHouseDialect).find_all(exp.ColumnDef))
        return Result(
            [row for obj in objects for row in obj.rows],
            [column.name for column in columns],
            [column.kind.sql(dialect=ClickHouseDialect) for column in columns],
        )

    def _select(self, statement: Any, settings: Any) -> Result:
        data = self._source_data(statement.args["from_"].this, settings)
        types = dict(zip(data.column_names, data.column_types))
        records = [dict(zip(data.column_names, row)) for row in data.result_rows]
        condition = statement.args.get("where")
        if condition is not None:
            records = [record for record in records if _value(condition.this, record, 0, 0)]
        expressions = statement.expressions
        if any(isinstance(item, exp.Star) for item in expressions):
            names, kinds = list(data.column_names), list(data.column_types)
            rows = [tuple(record[name] for name in names) for record in records]
        else:
            names = [item.alias_or_name for item in expressions]
            kinds = [_native_type(item, types) for item in expressions]
            group = statement.args.get("group")
            if group is not None or any(item.find(exp.Count) is not None for item in expressions):
                groups: dict[tuple[Any, ...], list[Any]] = (
                    {} if group is not None else {(): records}
                )
                for record in records if group is not None else []:
                    key = tuple(_value(item, record, 0, 0) for item in group.expressions)
                    groups.setdefault(key, []).append(record)
                rows = [
                    tuple(
                        _value(item, values[0] if values else {}, 0, len(values))
                        for item in expressions
                    )
                    for values in groups.values()
                ]
            else:
                rows = [
                    tuple(_value(item, record, ordinal, 0) for item in expressions)
                    for ordinal, record in enumerate(records, 1)
                ]
        limit = statement.args.get("limit")
        if limit is not None:
            rows = rows[: int(limit.expression.this)]
        return Result(rows, names, kinds)

    def _insert(self, target: Any, rows: Any, settings: Any) -> None:
        rows = list(rows)
        super()._insert(target, rows, settings)
        table = target.this if isinstance(target, exp.Schema) else target
        function = table.this if isinstance(table, exp.Table) else table
        if isinstance(function, exp.Anonymous):
            _cluster, database, relation, _trailing = cluster_arguments(function, "db")
            name = f"{database}.{relation}"
        else:
            assert isinstance(table, exp.Table)
            name = f"{table.db or 'db'}.{table.name}"
        if "__source" in name:
            self.cluster.populations.append((name, deepcopy(rows)))
            if self.cluster.change_scope:
                self.cluster.change_pending = True


def install_snapshot_scope(monkeypatch: Any, write_sql_connections: Any, *, change_scope: bool):
    configuration = {
        "ch": {
            "type": "ch",
            "driver": "http",
            "host": "h1",
            "port": 8123,
            "database": "db",
            "user": "user",
            "password": "password",
            "cluster_routing": {"cluster": "core"},
            "transfer_staging_schema": "db",
            "ddl_defaults": {
                "regular": {
                    "create_distributed_pair": True,
                    "shard": {"engine": "MergeTree", "on_cluster": "core"},
                    "distributed": {
                        "engine_template": (
                            "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                        ),
                        "cluster": "core",
                        "on_cluster": "core",
                        "sharding_key": "rand()",
                    },
                },
                "staging": {
                    "create_distributed_pair": False,
                    "shard": {"engine": "MergeTree", "on_cluster": "core"},
                },
            },
        },
        "gp": {**deepcopy(DEFAULT_SQL_CONNECTIONS["gp"]), "transfer_staging_schema": "db"},
    }
    set_connections_path(write_sql_connections(configuration))
    cluster = SnapshotScopeCluster(change_scope=change_scope)
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", cluster.open)
    target = MemoryGreenplum()
    target.install(monkeypatch)
    return cluster, target
