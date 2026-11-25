"""Native resources and metadata SQL shared by the older upsert row fixtures."""

from __future__ import annotations

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import engine_sql
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_transfer_replacement import _Connection, _Result
from tests.sql._support.reconfigure_conversion_scope import ConversionScopeClient


class _MetadataState(ConversionScopeClient):
    def _metadata(self, name, hosts):
        if name == "system.databases":
            return [
                {"_shard_num": index, "name": "db", "engine": "Atomic"}
                for index, _host in enumerate(hosts, 1)
            ]
        records = super()._metadata(name, hosts)
        if name == "system.tables":
            for record in records:
                host = next(
                    host
                    for host in hosts
                    if self.server_identities[host][0] == record["serverUUID"]
                )
                value = self.host_tables[host][f"{record['database']}.{record['name']}"]
                record["engine_full"] = engine_sql(value.create)
        return records


class _MetadataConnection(_Connection):
    def _reference(self, target):
        if isinstance(target, exp.Schema) and target.this.name.casefold() in {
            "cluster",
            "clusterallreplicas",
        }:
            target = exp.Table(
                this=exp.Anonymous(this=target.this.name, expressions=target.expressions)
            )
        if (
            isinstance(target, exp.Table)
            and isinstance(target.this, exp.Identifier)
            and not target.db
        ):
            target = target.copy()
            target.set("db", exp.to_identifier("db"))
        return super()._reference(target)

    def _source(self, select):
        if select.args.get("from_") is None:
            return [
                {
                    **self.state._host_record(self.host),
                    "currentDatabase": "db",
                    "getMacro": "caller_a",
                }
            ], []
        return super()._source(select)


class UpsertMetadata:
    """Allocate real fake table UUIDs once and answer projections from those resources."""

    def __init__(self, *, local, physical_table="events_shard"):
        self.state = _MetadataState()
        state = self.state
        state.metadata_transform = None
        state.metadata_faults = []
        for tables in state.host_tables.values():
            tables.clear()
        state.host_tables["caller"] = {}
        state.host_commands["caller"] = []
        state.host_endpoints["caller"] = ("host_a", "10.0.0.3", 9001)
        state.server_identities["caller"] = ("00000000-0000-0000-0000-000000000003", 9001)
        state.clusters = {
            "caller_a": ("caller",),
            "storage_b": (state.remote,),
            "facade_c": ("caller",),
        }
        fields = "id Int64, month Int64, value Int64"
        for host in state.host_tables:
            for table in {"events", "events_shard", physical_table, "incoming", "payload"}:
                engine = (
                    f"Distributed('storage_b','db','{physical_table}',rand())"
                    if host == state.coordinator and table == "events" and not local
                    else "MergeTree PARTITION BY month ORDER BY id"
                )
                state._create(
                    host,
                    parse_one(
                        f"CREATE TABLE db.{table} ({fields}) ENGINE={engine}",
                        read=ClickHouseDialect,
                    ),
                )
        self.connection = _MetadataConnection(state, state.coordinator, native=True)

    def query(self, statement, *, settings=None):
        body = strip_query_label(statement)
        if body.startswith("SHOW CREATE TABLE "):
            table = parse_one(
                body[len("SHOW CREATE TABLE ") :], read=ClickHouseDialect, into=exp.Table
            )
            value = self.state.host_tables[self.state.coordinator][
                f"{table.db or 'db'}.{table.name}"
            ]
            return _Result([(value.create.sql(dialect=ClickHouseDialect),)], ["statement"])
        if body.startswith("EXISTS TABLE "):
            table = parse_one(body[len("EXISTS TABLE ") :], read=ClickHouseDialect, into=exp.Table)
            exists = (
                f"{table.db or 'db'}.{table.name}" in self.state.host_tables[self.state.coordinator]
            )
            return _Result([(int(exists),)], ["result"])
        tree = parse_one(body, read=ClickHouseDialect)
        if isinstance(tree, exp.Describe):
            return self.connection.query(statement, settings=settings)
        tables = list(tree.find_all(exp.Table))
        if not tables or all(
            self.connection._reference(table)[1].startswith("system.") for table in tables
        ):
            return self.connection.query(statement, settings=settings)
        return None
