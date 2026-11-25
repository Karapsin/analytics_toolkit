"""Raw lifecycle commands over exact, independently owned endpoint resources."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import _DropConnection, install_drop_scope
from tests.sql._support.ch_transfer_replacement import (
    _parse_command,
    _Result,
    _validate_memory_storage,
)
from tests.sql._support.reconfigure_conversion_scope import _name, _scope_sql


class TableAlreadyExistsError(RuntimeError):
    code = 57


class UnknownColumnError(RuntimeError):
    code = 47


def _validate_storage_columns(create):
    available = {item.name for item in create.this.expressions if isinstance(item, exp.ColumnDef)}
    properties = create.args.get("properties")
    for item in properties.expressions if properties is not None else []:
        if not isinstance(item, (exp.Order, exp.PartitionedByProperty)):
            continue
        if any(column.name not in available for column in item.find_all(exp.Column)):
            message = "Storage key contains an unknown column."
            raise UnknownColumnError(message)


class LifecycleConnection(_DropConnection):
    def query(self, statement, settings=None, **kwargs):
        body = strip_query_label(statement)
        prefix = "DESCRIBE TABLE ("
        if not body.startswith(prefix):
            return super().query(statement, settings=settings, **kwargs)
        assert not self.closed
        expression = parse_one(body[len("DESCRIBE TABLE ") :], read=ClickHouseDialect)
        while isinstance(expression, (exp.Subquery, exp.Paren)):
            expression = expression.this
        assert isinstance(expression, exp.Select)
        columns = []
        for projection in expression.expressions:
            value = projection.this if isinstance(projection, exp.Alias) else projection
            assert isinstance(value, exp.Cast)
            columns.append(
                (projection.alias_or_name, value.args["to"].sql(dialect=ClickHouseDialect), "")
            )
        self.state.queries.append(statement)
        self.state.query_settings.append(deepcopy(settings))
        self.state.query_events.append((self.host, len(self.state.commands), statement))
        return _Result(columns, ["name", "type", "default_type"], ["String"] * 3)

    def _create_tables(self, tree, hosts):
        _validate_memory_storage(tree)
        _validate_storage_columns(tree)
        name = _name(tree.this.this)
        for host in hosts:
            if name in self.state.host_tables[host]:
                if tree.args.get("exists"):
                    continue
                message = "ClickHouse table already exists."
                raise TableAlreadyExistsError(message)
            self.state._create(host, tree)

    def _reference(self, target):
        if isinstance(target, exp.Schema) and target.this.name.casefold() in {
            "cluster",
            "clusterallreplicas",
        }:
            target = exp.Table(
                this=exp.Anonymous(this=target.this.name, expressions=target.expressions)
            )
        return super()._reference(target)

    def insert_df(self, table, df, column_names, **kwargs):
        return self.insert(
            table, list(df.itertuples(index=False, name=None)), column_names, **kwargs
        )

    def insert(self, table, data, column_names, **kwargs):
        if not table.lstrip().upper().startswith("FUNCTION "):
            return super().insert(table, data, column_names, **kwargs)
        assert not self.closed
        rows = deepcopy(list(data))
        columns = list(column_names)
        self.state.inserts.append(
            {
                "host": self.host,
                "table": table,
                "rows": rows,
                "columns": columns,
                "options": deepcopy(kwargs),
            }
        )
        column_sql = ", ".join(
            exp.to_identifier(column, quoted=True).sql(dialect=ClickHouseDialect)
            for column in columns
        )
        statement = _parse_command(f"INSERT INTO {table} ({column_sql}) SELECT 1")
        self._insert(statement.this, rows, columns)
        return None

    def command(self, statement, settings=None):
        cluster, body = _scope_sql(strip_query_label(statement))
        if not body.lstrip().upper().startswith("TRUNCATE "):
            return super().command(statement, settings=settings)
        assert not self.closed
        tree = parse_one(body, read=ClickHouseDialect)
        assert isinstance(tree, exp.TruncateTable)
        hosts = self.state.clusters[cluster] if cluster is not None else (self.host,)
        self.state.commands.append(statement)
        self.state.command_settings.append(deepcopy(settings))
        for host in hosts:
            self.state.host_commands[host].append(statement)
            for table in tree.find_all(exp.Table):
                name = _name(table)
                if name not in self.state.host_tables[host]:
                    assert tree.args.get("exists")
                    continue
                self.state.host_tables[host][name].rows.clear()
        return None


def install_lifecycle(
    monkeypatch, write_sql_connections, *, driver, cluster_routing=None, staging_cluster=None
):
    state = install_drop_scope(
        monkeypatch,
        write_sql_connections,
        driver=driver,
        cluster_routing=cluster_routing,
        staging_cluster=staging_cluster,
    )
    state.connection_class = LifecycleConnection
    return state


def install_external_facade(state):
    physical = parse_one(
        "CREATE TABLE analytics.external_storage (id UInt64) ENGINE=MergeTree ORDER BY id",
        read=ClickHouseDialect,
    )
    facade = parse_one(
        "CREATE TABLE analytics.events (id UInt64) "
        "ENGINE=Distributed('storage','analytics','external_storage',rand())",
        read=ClickHouseDialect,
    )
    state._create(state.remote, physical, [(value,) for value in range(100)])
    state.host_tables[state.coordinator].pop("analytics.events")
    state._create(state.coordinator, facade)
    state.created.clear()
