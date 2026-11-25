"""Ordinary execute-create preserves the selected healthy creation scope."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.routing import _RoutingClickHouse
from sqlglot import exp, parse_one

from tests.sql._support import outage as outage_support

_TARGET = "db.only_healthy_created"


@pytest.fixture
def observed_outage(monkeypatch, outage):
    cluster, configure = outage
    opened = []

    class CountingClient(outage_support.Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            opened.append(self)

        def close(self):
            self.close_calls += 1
            super().close()

        def query(self, statement, **kwargs):
            settings = kwargs.get("settings") or {}
            if settings.get("skip_unavailable_shards") == 0:
                parsed = parse_one(statement, read=_RoutingClickHouse)
                for function in parsed.find_all(exp.Anonymous):
                    if function.name.casefold() != "clusterallreplicas":
                        continue
                    name, _database, _table, _trailing = cluster_arguments(function, "db")
                    members = self.cluster.members_by_cluster.get(name, self.cluster.members)
                    missing = [
                        host
                        for _shard, _replica, host, _port in members
                        if host not in self.cluster.alive
                    ]
                    if missing:
                        # An explicit non-skipping server query cannot silently omit h2.
                        self.cluster.queries.append(statement)
                        self.cluster.probe_requests.append(dict(settings))
                        message = f"Unavailable cluster endpoint: {', '.join(missing)}."
                        raise ConnectionError(message)
            return super().query(statement, **kwargs)

    monkeypatch.setattr(outage_support, "Client", CountingClient)
    scope = {
        "create_distributed_pair": False,
        "shard": {"engine": "MergeTree", "on_cluster": "core"},
    }
    configure(
        cluster_routing=None,
        ddl_defaults={"regular": scope, "staging": deepcopy(scope)},
    )
    cluster.alive.remove("h2")
    assert all((host, _TARGET) not in cluster.tables for host in cluster.hosts)
    return cluster, opened


def _create(*, if_not_exists, policy):
    return sql.execute_create(
        "ch",
        _TARGET,
        "SELECT 17 AS id",
        if_not_exists=if_not_exists,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        ch_shard_on_cluster="core",
        ch_unavailable_shards=policy,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )


def _assert_closed_once(opened):
    assert opened
    assert all(client.closed for client in opened)
    assert all(client.close_calls == 1 for client in opened)


@pytest.mark.parametrize("if_not_exists", [False, True])
def test_execute_create_healthy_new_target_uses_only_selected_hosts(observed_outage, if_not_exists):
    cluster, opened = observed_outage
    before = deepcopy(cluster.tables)

    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        result = _create(if_not_exists=if_not_exists, policy="healthy_shards")

    assert isinstance(result.rows, int)
    assert result.metadata.statement_count > 0
    assert cluster.tables[("h1", _TARGET)].rows == [(17,)]
    assert ("h2", _TARGET) not in cluster.tables
    assert {key: value for key, value in cluster.tables.items() if key[1] != _TARGET} == before
    assert cluster.commands
    assert all(host == "h1" for host, _query, _settings in cluster.commands)
    assert all("ON CLUSTER" not in query.upper() for _host, query, _settings in cluster.commands)
    assert result.metadata.data_complete is False
    status = next(item for item in result.metadata.ch_cluster_status if item["table"] == _TARGET)
    assert status["host_coverage_complete"] is False
    assert status["selected_shards"] == [1]
    _assert_closed_once(opened)


@pytest.mark.parametrize("if_not_exists", [False, True])
def test_execute_create_stop_new_target_rejects_incomplete_hosts_before_ddl(
    observed_outage, if_not_exists
):
    cluster, opened = observed_outage
    before = deepcopy(cluster.tables)

    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        _create(if_not_exists=if_not_exists, policy="stop")

    assert cluster.tables == before
    assert cluster.commands == []
    _assert_closed_once(opened)
