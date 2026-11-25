"""Real drop adapter connections over independently owned ClickHouse resources."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.outage_context import INTERNAL_IO
from analytics_toolkit.sql.execution.observation import observed_connection
from sqlglot import exp, parse_one
from sqlglot.tokens import TokenType

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_transfer_replacement import ReplacementScopeState, _Connection
from tests.sql._support.reconfigure_conversion_scope import _name, _scope_sql


class _DropConnection(_Connection):
    def __init__(self, state, host, *, native):
        super().__init__(state, host, native=native)
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        super().close()

    def query(self, statement, settings=None, **kwargs):
        self.state.query_events.append((self.host, len(self.state.commands), statement))
        if self.state.before_query is not None:
            self.state.before_query(self, statement)
        result = super().query(statement, settings=settings, **kwargs)
        if self.state.after_query is not None:
            self.state.after_query(self, statement, result)
        return result

    def command(self, statement, settings=None):
        assert not self.closed
        if self._delay_drop(statement, settings):
            return None
        return super().command(statement, settings=settings)

    def _delay_drop(self, statement, settings):
        cluster, body = _scope_sql(strip_query_label(statement))
        if cluster is None or not self.state.delay_cluster_drops:
            return False
        tokens = ClickHouseDialect().tokenize(body)
        if not tokens or tokens[0].token_type != TokenType.DROP:
            return False
        tree = parse_one(body, read=ClickHouseDialect)
        assert isinstance(tree, exp.Drop)
        targets = tree.args.get("tables") or [tree.this]
        if len(targets) != 1:
            return False
        key = cluster, _name(targets[0])
        if key not in self.state.delay_cluster_drops:
            return False
        self.state.delay_cluster_drops.remove(key)
        self.state.delayed_cluster_drops.append(key)
        self.state.commands.append(statement)
        self.state.command_settings.append(deepcopy(settings))
        for host in self.state.clusters[cluster]:
            self.state.host_commands[host].append(statement)
        return True


class DropScopeState(ReplacementScopeState):
    connection_class = _DropConnection

    def __init__(self):
        super().__init__()
        self.query_events = []
        self.opened_configs = []
        self.before_query = None
        self.after_query = None
        self.delay_cluster_drops = set()
        self.delayed_cluster_drops = []

    def open(self, config, **_kwargs):
        self.opened_configs.append(deepcopy(config))
        matches = [
            host
            for host, (name, address, port) in self.host_endpoints.items()
            if config.host in {host, name, address}
            and (config.driver != "native" or config.port == port)
        ]
        assert len(matches) == 1, (config.host, matches)
        raw = self.connection_class(self, matches[0], native=config.driver == "native")
        self.opened.append(raw)
        if INTERNAL_IO.get():
            return raw
        wrapped = OutageClient(observed_connection(raw), config)
        self.wrappers.append(wrapped)
        return wrapped


def install_drop_scope(  # noqa: PLR0913 - preserve shared fixture defaults and call shapes.
    monkeypatch,
    write_sql_connections,
    *,
    driver="http",
    connection_key="ch",
    cluster_routing=None,
    staging_cluster=None,
):
    assert driver in {"http", "native"}
    path = write_sql_connections(
        {
            connection_key: {
                "type": "ch",
                "driver": driver,
                "host": "coordinator",
                "port": 9000 if driver == "native" else 8123,
                "user": "user",
                "password": "password",
                "database": "analytics",
                "cluster_routing": cluster_routing,
                "ddl_defaults": {
                    "regular": {
                        "create_distributed_pair": True,
                        "shard": {"engine": "MergeTree", "on_cluster": "storage"},
                        "distributed": {
                            "engine_template": (
                                "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                            ),
                            "cluster": "storage",
                            "on_cluster": None,
                            "sharding_key": "rand()",
                        },
                    },
                    "staging": {
                        "create_distributed_pair": False,
                        "shard": {"engine": "Memory", "on_cluster": staging_cluster},
                    },
                },
            }
        }
    )
    set_connections_path(path)
    state = DropScopeState()
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", state.open)
    return state
