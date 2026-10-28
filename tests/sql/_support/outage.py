"""Small in-memory two-shard server used through the real SQL public facade."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pandas as pd
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.outage_context import INTERNAL_IO
from analytics_toolkit.sql.backends.ch.routing import _RoutingClickHouse
from sqlglot import exp, parse_one


class Result:
    def __init__(self, rows: Any, names: Any = ("id",), kinds: Any = ("UInt64",)) -> None:
        self.result_rows = list(rows)
        self.column_names = tuple(names)
        self.column_types = tuple(kinds)
        self.result_columns = (
            list(zip(*self.result_rows)) if self.result_rows else [() for _ in names]
        )
        self.row_count = len(self.result_rows)


@dataclass
class Table:
    ddl: str
    engine: str
    engine_full: str
    uuid: str
    rows: list[tuple[Any, ...]] = field(default_factory=list)


class Cluster:
    def __init__(self) -> None:
        self.hosts = ["h1", "h2"]
        self.alive = {"h1", "h2"}
        self.members = [(1, 1, "h1", 9000), (2, 1, "h2", 9000)]
        self.tables: dict[tuple[str, str], Table] = {}
        self.commands: list[tuple[str, str, dict[str, Any]]] = []
        self.queries: list[str] = []
        self.selected_reads: list[tuple[str, str]] = []
        self.connections: list[tuple[str, int]] = []
        self.database_engine = "Atomic"
        self.members_by_cluster: dict[str, list[Any]] = {}
        self.replica_defaults = {
            "default_replica_path": "/tables/{uuid}/{shard}",
            "default_replica_name": "{replica}",
        }
        self.host_macros: dict[str, dict[str, str]] = {}
        self.failure: Exception | None = None
        self.replica_readonly: set[str] = set()
        self.replica_expired: set[str] = set()
        self.probe_failure: Exception | None = None

    def seed(self, *, key: str = "rand()", replicated: bool = False) -> None:
        identity = str(uuid4())
        for host in self.hosts:
            engine = "ReplicatedMergeTree" if replicated else "MergeTree"
            self.create(
                host,
                f"CREATE TABLE db.events_shard UUID '{identity}' "
                f"(id UInt64) ENGINE={engine} ORDER BY id",
            )
            self.create(
                host,
                "CREATE TABLE db.events (id UInt64) "
                f"ENGINE=Distributed('core', 'db', 'events_shard', {key})",
            )
        self.tables[("h1", "db.events_shard")].rows = [(1,)]
        self.tables[("h2", "db.events_shard")].rows = [(2,)]

    def create(self, host: str, sql: str) -> None:
        statement = parse_one(sql, read=_RoutingClickHouse)
        target = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
        name = f"{target.db or 'db'}.{target.name}"
        if (host, name) in self.tables:
            return
        engine = statement.find(exp.EngineProperty).this
        uuid = statement.find(exp.UuidProperty)
        engine_name = engine.name if isinstance(engine, exp.Identifier) else str(engine.this)
        statement.set("exists", False)
        properties = statement.args.get("properties")
        if properties is not None:
            properties.set(
                "expressions",
                [item for item in properties.expressions if not isinstance(item, exp.OnCluster)],
            )
        self.tables[(host, name)] = Table(
            statement.sql(dialect="clickhouse"),
            engine_name,
            engine.sql(dialect="clickhouse"),
            str(uuid.this.this) if uuid else str(uuid4()),
        )

    def open(self, config: Any, **_kwargs: Any) -> Any:
        self.connections.append((config.host, config.port))
        if config.host not in self.alive:
            msg = "host unavailable"
            raise ConnectionError(msg)
        raw = Client(self, config.host, native=config.driver == "native")
        return raw if INTERNAL_IO.get() else OutageClient(raw, config)


class Client:
    def __init__(self, cluster: Cluster, host: str, *, native: bool = False) -> None:
        self.cluster, self.host = cluster, host
        self.is_native_transport = native
        self.closed = False

    def query(self, sql: str, **kwargs: Any) -> Result:  # noqa: C901, PLR0911, PLR0912 - fake SQL dispatcher
        self.cluster.queries.append(sql)
        if sql.startswith("EXISTS TABLE "):
            return Result(
                [
                    (
                        int(
                            (self.host, sql[len("EXISTS TABLE ") :].replace("`", ""))
                            in self.cluster.tables
                        ),
                    )
                ]
            )
        statement = parse_one(sql, read=_RoutingClickHouse)
        filters = {
            item.left.name: str(item.right.this)
            for item in statement.find_all(exp.EQ)
            if isinstance(item.right, exp.Literal)
        }
        upper = sql.upper()
        if "GETMACRO(" in upper:
            return Result([("core",)])
        if "FROM SYSTEM.CLUSTERS" in upper:
            return Result(
                self.cluster.members_by_cluster.get(
                    filters.get("cluster", "core"), self.cluster.members
                )
            )
        if "FROM SYSTEM.SERVER_SETTINGS" in upper:
            return Result(list(self.cluster.replica_defaults.items()))
        if "FROM SYSTEM.MACROS" in upper:
            index = self.cluster.hosts.index(self.host)
            return Result(
                list(
                    self.cluster.host_macros.get(
                        self.host,
                        {
                            "shard": str(self.cluster.members[index][0]),
                            "replica": self.host,
                        },
                    ).items()
                )
            )
        if "FROM SYSTEM.TABLES" in upper:
            name = f"{filters.get('database', 'db')}.{filters.get('name', '')}"
            table = self.cluster.tables.get((self.host, name))
            if table is None:
                return Result([])
            if "SELECT ENGINE_FULL FROM" in upper:
                return Result([(table.engine_full,)])
            return Result([(table.engine, table.engine_full, table.ddl, table.uuid)])
        if "FROM SYSTEM.SETTINGS" in upper:
            return Result([])
        function = next(
            (
                item
                for item in statement.find_all(exp.Anonymous)
                if item.name.lower() in {"cluster", "clusterallreplicas"}
            ),
            None,
        )
        if function is None and isinstance(statement, exp.Select):
            function = self._native_distributed_function(statement)
        cluster_name = str(function.expressions[0].this) if function is not None else "core"
        members = self.cluster.members_by_cluster.get(cluster_name, self.cluster.members)
        hosts = [item[2] for item in members]
        if "SYSTEM, ONE" in upper:
            if self.cluster.probe_failure:
                raise self.cluster.probe_failure
            return Result(
                [(i + 1, host) for i, host in enumerate(hosts) if host in self.cluster.alive]
            )
        if "SYSTEM, DATABASES" in upper:
            return Result(
                [
                    (i + 1, self.cluster.database_engine)
                    for i, host in enumerate(hosts)
                    if host in self.cluster.alive
                ]
            )
        if "SYSTEM, REPLICAS" in upper:
            name = f"{filters.get('database', 'db')}.{filters.get('table', '')}"
            return Result(
                [
                    (
                        i + 1,
                        f"/tables/{self.cluster.tables[(host, name)].uuid}/{members[i][0]}",
                        host,
                        int(host in self.cluster.replica_readonly),
                        int(host in self.cluster.replica_expired),
                    )
                    for i, host in enumerate(hosts)
                    if host in self.cluster.alive and (host, name) in self.cluster.tables
                ]
            )
        if "SYSTEM, TABLES" in upper:
            name = f"{filters.get('database', 'db')}.{filters.get('name', '')}"
            return Result(
                [
                    (i + 1, obj.engine, obj.engine_full, obj.ddl, obj.uuid)
                    for i, host in enumerate(hosts)
                    if host in self.cluster.alive
                    and (obj := self.cluster.tables.get((host, name))) is not None
                ]
            )
        if "FROM SYSTEM.TABLES" in upper:
            name = f"{filters.get('database', 'db')}.{filters.get('name', '')}"
            obj = self.cluster.tables.get((self.host, name))
            return Result([(obj.engine_full,)] if obj else [])
        if "FROM SYSTEM.COLUMNS" in upper or "SYSTEM, COLUMNS" in upper:
            return Result([("id", "UInt64")], ("name", "type"), ("String", "String"))
        if isinstance(statement, exp.Describe):
            return self._describe(statement)
        function = next(
            (
                item
                for item in statement.find_all(exp.Anonymous)
                if item.name.lower() in {"cluster", "clusterallreplicas"}
            ),
            None,
        )
        if function is not None:
            args = function.expressions
            name = f"{args[1].this}.{args[2].this}"
            selected = self._select_hosts(function, members, name, kwargs.get("settings") or {})
            self.cluster.selected_reads.extend((host, name) for host in selected)
            rows = [
                row
                for host in selected
                for row in self.cluster.tables.get((host, name), Table("", "", "", "")).rows
            ]
            if statement.find(exp.Count):
                if statement.args.get("group") is not None:
                    return Result(
                        [(0, len(rows))] if rows else [],
                        ("slice_id", "count"),
                        ("UInt64", "UInt64"),
                    )
                return Result([(len(rows),)])
            obj = next(
                (
                    self.cluster.tables[(host, name)]
                    for host in hosts
                    if (host, name) in self.cluster.tables
                ),
                None,
            )
            return self._project(statement, rows, obj)
        if statement.find(exp.Count):
            return Result([(1,)])
        if "LIMIT 0" in upper:
            return Result([])
        return Result([(1,)])

    def _native_distributed_function(self, statement: Any) -> Any:
        for table in statement.find_all(exp.Table):
            obj = self.cluster.tables.get((self.host, f"{table.db or 'db'}.{table.name}"))
            if obj is not None and obj.engine == "Distributed":
                engine = parse_one(obj.engine_full, read=_RoutingClickHouse)
                return exp.Anonymous(this="cluster", expressions=engine.expressions)
        return None

    def _select_hosts(self, function: Any, members: Any, name: str, settings: Any) -> list[str]:
        selected = [host for _shard, _replica, host, _port in members if host in self.cluster.alive]
        if function.name.lower() == "cluster":
            per_shard: dict[int, str] = {}
            for shard, _replica, host, _port in members:
                if host in self.cluster.alive:
                    per_shard.setdefault(shard, host)
            # Replica selection precedes table readiness. Skipping a missing
            # table can therefore drop a whole shard, not choose its other replica.
            selected = list(per_shard.values())
            if any((host, name) not in self.cluster.tables for host in selected):
                if not settings.get("skip_unavailable_shards"):
                    msg = f"Selected reachable replica is missing {name}."
                    raise RuntimeError(msg)
                selected = [host for host in selected if (host, name) in self.cluster.tables]
        return selected

    def _describe(self, statement: Any) -> Result:
        target = statement.this
        if isinstance(target, exp.Schema):
            target = (
                exp.Anonymous(this=target.this.name, expressions=target.expressions)
                if target.this.name.lower() in {"cluster", "clusterallreplicas"}
                else target.this
            )
        if not isinstance(target, (exp.Table, exp.Anonymous)):
            target = next(statement.find_all(exp.Table), None)
        if not isinstance(target, (exp.Table, exp.Anonymous)):
            return Result([("id", "UInt64")], ("name", "type"), ("String", "String"))
        function = target if isinstance(target, exp.Anonymous) else target.this
        name = (
            f"{function.expressions[1].this}.{function.expressions[2].this}"
            if isinstance(function, exp.Anonymous)
            else f"{target.db or 'db'}.{target.name}"
        )
        obj = self.cluster.tables.get((self.host, name))
        if obj:
            columns = parse_one(obj.ddl, read=_RoutingClickHouse).find_all(exp.ColumnDef)
            return Result(
                [(item.name, item.args["kind"].sql(dialect="clickhouse")) for item in columns],
                ("name", "type"),
                ("String", "String"),
            )
        return Result([("id", "UInt64")], ("name", "type"), ("String", "String"))

    @staticmethod
    def _project(statement: Any, rows: Any, obj: Any) -> Result:
        fields = (
            [
                item.name
                for item in parse_one(obj.ddl, read=_RoutingClickHouse).find_all(exp.ColumnDef)
            ]
            if obj
            else ["id"]
        )
        selected = statement
        if any(isinstance(item, exp.Star) for item in selected.expressions):
            selected = next(
                (
                    item
                    for item in statement.find_all(exp.Select)
                    if any(isinstance(value, exp.Alias) for value in item.expressions)
                ),
                selected,
            )
        expressions = selected.expressions
        if any(isinstance(item, exp.Star) for item in expressions):
            return Result(rows, fields, ["UInt64"] * len(fields))
        names = [item.alias_or_name for item in expressions]
        projected = []
        for ordinal, row in enumerate(rows, 1):
            record = dict(zip(fields, row))
            values = []
            for item in expressions:
                expression = item.this if isinstance(item, exp.Alias) else item
                while isinstance(expression, exp.Cast):
                    expression = expression.this
                if isinstance(expression, exp.Column):
                    values.append(record[expression.name])
                elif isinstance(expression, exp.Literal):
                    values.append(int(expression.this))
                else:
                    assert isinstance(expression, exp.Window)
                    values.append(ordinal)
            projected.append(tuple(values))
        return Result(projected, names, ["UInt64"] * len(names))

    def query_df(self, sql: str, **kwargs: Any) -> pd.DataFrame:
        result = self.query(sql, **kwargs)
        return pd.DataFrame(result.result_rows, columns=result.column_names)

    def query_df_stream(self, sql: str, **kwargs: Any) -> Any:
        return nullcontext(iter([self.query_df(sql, **kwargs)]))

    def command(self, sql: str, **kwargs: Any) -> Any:
        self.cluster.commands.append((self.host, sql, dict(kwargs.get("settings") or {})))
        statement = parse_one(sql, read=_RoutingClickHouse)
        if isinstance(statement, exp.Create):
            self.cluster.create(self.host, sql)
        elif isinstance(statement, exp.Drop):
            target = statement.this or statement.args["tables"][0]
            self.cluster.tables.pop((self.host, f"{target.db or 'db'}.{target.name}"), None)
        elif isinstance(statement, exp.TruncateTable):
            for target in statement.find_all(exp.Table):
                self.cluster.tables[(self.host, f"{target.db or 'db'}.{target.name}")].rows.clear()
            if self.cluster.failure is not None:
                raise self.cluster.failure
        elif isinstance(statement, exp.Insert):
            rows = [
                (int(next(item.find_all(exp.Literal)).this),)
                for item in statement.expression.expressions
            ]
            self._insert(statement.this, rows, kwargs.get("settings") or {})
        elif self.cluster.failure is not None:
            raise self.cluster.failure
        return None

    def _insert(self, target: Any, rows: Any, settings: Any) -> None:
        if isinstance(target, exp.Schema):
            target = target.this
        function = target if isinstance(target, exp.Anonymous) else target.this
        if isinstance(function, exp.Anonymous):
            arguments = function.expressions
            name = f"{arguments[1].this}.{arguments[2].this}"
            shard = int(settings.get("insert_shard_id", 1))
            host = self.cluster.hosts[shard - 1]
        else:
            name = f"{target.db or 'db'}.{target.name}"
            obj = self.cluster.tables[(self.host, name)]
            if obj.engine == "Distributed":
                name += "_shard"
            host = self.host
        if host not in self.cluster.alive:
            msg = "shard unavailable"
            raise ConnectionError(msg)
        self.cluster.tables[(host, name)].rows.extend(rows)
        if self.cluster.failure is not None:
            raise self.cluster.failure

    def create_insert_context(self, *, table: str, **kwargs: Any) -> Any:
        return SimpleNamespace(table=table, settings=kwargs.get("settings"), data=None)

    def data_insert(self, context: Any) -> None:
        rows = (
            list(context.data.itertuples(index=False, name=None))
            if isinstance(context.data, pd.DataFrame)
            else context.data
        )
        self.insert(context.table, rows, column_names=["id"], settings=context.settings)

    def insert(self, table: str, data: Any, column_names: Any, **kwargs: Any) -> None:
        self.cluster.commands.append(
            (self.host, f"INSERT INTO {table}", kwargs.get("settings") or {})
        )
        target = parse_one(f"INSERT INTO {table} VALUES (0)", read=_RoutingClickHouse).this
        self._insert(target, data, kwargs.get("settings") or {})

    def insert_df(self, table: str, df: Any, column_names: Any, **kwargs: Any) -> None:
        self.insert(table, list(df.itertuples(index=False, name=None)), column_names, **kwargs)

    def close(self) -> None:
        self.closed = True
