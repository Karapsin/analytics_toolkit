"""Exact CH catalog state and fresh handles for legacy transfer contracts."""

from __future__ import annotations

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.create_from_sql_protocol import CatalogClickHouseClient
from tests.sql._support.transfer_driver_boundary import install_transfer_factories


class TransferClickHouseClient(CatalogClickHouseClient):
    def _reference(self, target):
        # Raw ClickHouse evaluates currentDatabase() inside a cluster table
        # function; routing's deliberately static source parser runs earlier.
        if target.find(exp.CurrentDatabase) is not None:
            target = target.copy()
            for current in target.find_all(exp.CurrentDatabase):
                current.replace(exp.Literal.string("analytics"))
        return super()._reference(target)

    def _metadata(self, name, hosts):
        records = (
            [
                {"_shard_num": index, "macro": "cluster", "substitution": "storage"}
                for index, _host in enumerate(hosts, 1)
            ]
            if name == "system.macros"
            else super()._metadata(name, hosts)
        )
        if name == "system.databases":
            records.extend(
                {**row, "name": database}
                for row in list(records)
                for database in ("sandbox", "analytics_toolkit_transfer")
            )
        return [{**row, "currentDatabase": "analytics"} for row in records]

    def _insert(self, target, rows, columns=None):
        rows = list(rows)
        hosts, name = self._reference(target)
        value = self.state.host_tables[hosts[0]][name]
        if value.route is None or len(self.state.clusters[value.route[0]]) != 1:
            return super()._insert(target, rows, columns)
        if isinstance(target, exp.Schema):
            columns = [item.name for item in target.expressions]
        if columns is not None:
            assert set(columns) == set(value.schema)
            rows = [tuple(dict(zip(columns, row))[name] for name in value.schema) for row in rows]
        # Any native sharding function selects endpoint zero for a singleton
        # shard set, including noninteger keys such as Date.
        cluster, physical = value.route
        self._written_rows = len(rows)
        return self.state.insert_rows(self.state.clusters[cluster][0], physical, rows)

    @property
    def queries(self):
        return self.state.queries

    @property
    def inserts(self):
        return [
            {
                "table": item["table"],
                "data": item["rows"],
                "column_names": item["columns"],
                "column_type_names": item["options"].get("column_type_names"),
                **({"df": item["dataframe"]} if "dataframe" in item else {}),
            }
            for item in self.state.inserts
        ]

    def insert_df(self, table, df, column_names, **kwargs):
        first = len(self.state.inserts)
        result = super().insert_df(table, df, column_names, **kwargs)
        for item in self.state.inserts[first:]:
            item["dataframe"] = df.copy()
        return result


def install_legacy_transfer_connections(monkeypatch, source, target):
    opened = {"gp": 0, "ch": 0}

    def connect(key):
        opened[key] += 1
        if key == "gp":
            return source if opened[key] == 1 else type(source)(source._rows, source._description)
        if key == "ch":
            return target if opened[key] == 1 else type(target)(target.state)
        message = "Transfer requested a connection outside its isolated fixture."
        raise AssertionError(message)

    install_transfer_factories(monkeypatch, connect)


def command_trees(client, operation, relation):
    result = []
    for statement in client.commands:
        body = strip_query_label(statement)
        if not body.startswith(operation.upper() + " "):
            continue
        tree = parse_one(body, read=ClickHouseDialect)
        table = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        if isinstance(table, exp.Table) and table.name == relation:
            result.append(tree)
    return result


def column_types(create):
    return {
        column.name: column.kind.sql(dialect=ClickHouseDialect)
        for column in create.this.expressions
        if isinstance(column, exp.ColumnDef)
    }
