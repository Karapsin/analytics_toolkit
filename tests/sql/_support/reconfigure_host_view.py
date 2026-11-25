"""Endpoint-specific raw connections over the conversion fixture's shared state."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from sqlglot import exp

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_transfer_replacement import _Connection
from tests.sql._support.reconfigure_metadata import ReconfigureMetadata, _endpoint


class ConversionHostView(_Connection):
    def __init__(self, state, host, *, native):
        super().__init__(state, host, native=native)
        self.close_calls = 0
        self.query_events = []

    def close(self):
        self.close_calls += 1
        super().close()

    def query(self, statement, settings=None, **kwargs):
        guard = getattr(self.state, "host_query_guard", None)
        if guard is not None:
            guard(statement)
        self.query_events.append((statement, deepcopy(settings)))
        result = super().query(statement, settings=settings, **kwargs)
        transform = getattr(self.state, "transform_response", None)
        return transform(statement, result) if transform is not None else result

    def _reference(self, target):
        function = target.this if isinstance(target, (exp.Table, exp.Schema)) else target
        clusters = getattr(self.state, "node_clusters", None)
        if clusters is not None and isinstance(function, exp.Anonymous):
            assert function.name.casefold() in {"cluster", "clusterallreplicas"}
            cluster, database, relation, _trailing = cluster_arguments(function, "analytics")
            resolved = self.state._resolved_cluster(self.host, cluster)
            return clusters[self.host][resolved], f"{database}.{relation}"
        return super()._reference(target)

    def _source(self, select):
        records, columns = super()._source(select)
        if select.args.get("from_") is None:
            calls = [
                item
                for item in select.find_all(exp.Anonymous)
                if item.name.casefold() == "getmacro"
            ]
            if calls and hasattr(self.state, "node_macros"):
                assert len(calls) == 1
                macro = calls[0].expressions[0].name
                records[0]["getMacro"] = self.state.node_macros[self.host][macro]
            else:
                records[0]["getMacro"] = "core"
        return records, columns

    def _metadata(self, name, hosts):
        records = super()._metadata(name, hosts)
        if name != "system.clusters" or not records or "_shard_num" in records[0]:
            return records
        return [
            {
                **record,
                "_shard_num": position,
                "is_local": int(
                    (record["host_address"] or record["host_name"], record["port"])
                    == (
                        self.state.host_endpoints[origin][1]
                        or self.state.host_endpoints[origin][0],
                        self.state.host_endpoints[origin][2],
                    )
                ),
            }
            for position, origin in enumerate(hosts, 1)
            for record in records
        ]


class _OriginMetadata(ReconfigureMetadata):
    def __init__(self, metadata, host):
        metadata._seed()
        super().__init__(metadata.client)
        self.tables = metadata.tables
        self.distribution_queues = metadata.distribution_queues
        self.seeded = True
        self.origin = host

    @property
    def coordinator(self):
        return self.origin


class ReconfigureMetadataHostView:
    def __init__(self, client, host, *, native):
        self.state = client
        self.host = host
        self.is_native_transport = native
        self.metadata = _OriginMetadata(client.metadata, host)
        self.close_calls = 0
        self.closed = False
        self.query_events = []

    def close(self):
        self.close_calls += 1
        self.closed = True

    def query(self, statement, settings=None):
        assert not self.closed
        self.query_events.append((statement, deepcopy(settings)))
        self.state.queries.append(statement)
        self.state.query_settings.append(deepcopy(settings))
        body = strip_query_label(statement)
        ddl = self.metadata.show_create(body)
        rows = [(ddl,)] if ddl is not None else self.metadata.rows(body)
        assert rows is not None, statement
        return SimpleNamespace(result_rows=rows)


def _host_endpoints(client):
    if hasattr(client, "host_endpoints"):
        return client.host_endpoints.items()
    return [(host, host) for host in (*client.source_hosts, *client.target_hosts)]


def install_conversion_host_views(monkeypatch, client):
    client.host_views = []
    if not hasattr(client, "metadata_transform"):
        client.metadata_transform = None
        client.metadata_faults = []

    def open_connection(config, **_kwargs):
        matches = {
            (address or name, port): host
            for host, (name, address, port) in _host_endpoints(client)
            if config.host in {name, address} and (config.driver != "native" or config.port == port)
        }
        assert len(matches) == 1, (config.host, config.port, matches)
        view = ConversionHostView if hasattr(client, "host_tables") else ReconfigureMetadataHostView
        host = next(iter(matches.values()))
        if not hasattr(client, "host_tables"):
            canonical = {_endpoint(item): item for item in reversed(client.source_hosts)}
            host = canonical.get(_endpoint(host), host)
        connection = view(client, host, native=config.driver == "native")
        client.host_views.append(connection)
        return connection

    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", open_connection)
