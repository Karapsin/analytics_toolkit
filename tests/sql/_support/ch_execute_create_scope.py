"""Real execute-create adapter over scoped, CTAS-capable ClickHouse endpoints."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.outage_context import INTERNAL_IO
from analytics_toolkit.sql.execution.observation import observed_connection
from sqlglot import exp

from tests.sql._support.ch_drop_scope import DropScopeState, _DropConnection, install_drop_scope
from tests.sql._support.reconfigure_conversion_scope import _name


def _literal_type(expression):
    literal = expression.unalias()
    assert isinstance(literal, exp.Literal)
    assert not literal.is_string
    value = int(literal.this)
    assert value >= 0
    for width in (8, 16, 32, 64):
        if value < 2**width:
            return f"UInt{width}"
    message = "The execute-create fixture requires a supported integer literal."
    raise AssertionError(message)


class _ExecuteCreateConnection(_DropConnection):
    def command(self, statement, settings=None):
        self._written_rows = None
        result = super().command(statement, settings=settings)
        if self._written_rows is not None:
            return {"written_rows": self._written_rows}
        return result

    def _insert(self, target, rows, columns=None):
        rows = list(rows)
        super()._insert(target, rows, columns)
        self._written_rows = len(rows)

    def _create_tables(self, tree, hosts):
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        name = _name(target)
        pending = [host for host in hosts if name not in self.state.host_tables[host]]
        if not pending and tree.args.get("exists"):
            return None
        if len(pending) != len(hosts) and not tree.args.get("exists"):
            message = f"Table {name} already exists."
            raise RuntimeError(message)
        source = tree.expression
        if isinstance(tree.this, exp.Schema):
            return super()._create_tables(tree, hosts)
        if isinstance(source, exp.Table):
            source_hosts, source_name = self._reference(source)
            original = self.state.host_tables[source_hosts[0]][source_name]
            columns = [column.copy() for column in original.create.this.expressions]
            rows = []
        else:
            assert isinstance(source, exp.Select)
            data = self._select(source)
            columns = [
                exp.ColumnDef(
                    this=exp.to_identifier(column, quoted=True),
                    kind=exp.DataType.build(_literal_type(expression), dialect=ClickHouseDialect),
                )
                for column, expression in zip(data.column_names, source.expressions)
            ]
            rows = [] if tree.find(exp.EmptyProperty) is not None else data.result_rows
        created = tree.copy()
        created.set("this", exp.Schema(this=target.copy(), expressions=columns))
        created.set("expression", None)
        properties = created.args.get("properties")
        if properties is not None:
            properties.set(
                "expressions",
                [
                    item
                    for item in properties.expressions
                    if not isinstance(item, exp.EmptyProperty)
                ],
            )
        for host in pending:
            self.state._create(host, created, rows)
        self._written_rows = len(rows) * len(pending)
        return None


class ExecuteCreateScopeState(DropScopeState):
    def open(self, config, **_kwargs):
        self.opened_configs.append(deepcopy(config))
        matches = [
            host
            for host, (name, address, port) in self.host_endpoints.items()
            if config.host in {host, name, address}
            and (config.driver != "native" or config.port == port)
        ]
        assert len(matches) == 1, (config.host, matches)
        raw = _ExecuteCreateConnection(self, matches[0], native=config.driver == "native")
        self.opened.append(raw)
        if INTERNAL_IO.get():
            return raw
        wrapped = OutageClient(observed_connection(raw), config)
        self.wrappers.append(wrapped)
        return wrapped


def install_execute_create_scope(monkeypatch, write_sql_connections, *, local_policy=False):
    def write_complete_defaults(connections):
        configured = deepcopy(connections)
        configured["ch"]["ddl_defaults"]["staging"] = {
            "create_distributed_pair": False,
            "shard": {"engine": "MergeTree", "on_cluster": None},
        }
        if local_policy:
            configured["ch"]["ddl_defaults"]["regular"] = {
                "create_distributed_pair": False,
                "shard": {"engine": "MergeTree", "on_cluster": None},
            }
        return write_sql_connections(configured)

    install_drop_scope(monkeypatch, write_complete_defaults)
    state = ExecuteCreateScopeState()
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", state.open)
    return state
