from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import general
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.sql._support.outage import Cluster


@pytest.fixture
def outage(monkeypatch: pytest.MonkeyPatch, write_sql_connections: Any) -> Any:
    cluster = Cluster()
    cluster.seed()

    def configure(*, aliases: tuple[str, ...] = ("ch",), **values: Any) -> Cluster:
        config = {
            "type": "ch",
            "host": "h1",
            "database": "db",
            "user": "user",
            "password": "password",
            "cluster_routing": {"cluster": "core"},
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
        }
        config.update(values)
        write_sql_connections(dict.fromkeys(aliases, config))
        general.set_connections_path(None)
        return cluster

    configure()
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", cluster.open)
    return cluster, configure
