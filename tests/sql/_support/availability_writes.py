"""Physical data and acknowledgement inspection for optional facade workflows."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from analytics_toolkit.sql.backends.ch import created_resources


def configure_optional_facade(outage: Any, transport: str, *, partial: bool) -> Any:
    cluster, configure = outage
    scope = {
        "create_distributed_pair": True,
        "shard": {"engine": "MergeTree", "on_cluster": "core"},
        "distributed": {
            "engine_template": "Distributed({cluster}, {database}, {shard_table}, {sharding_key})",
            "cluster": "core",
            "on_cluster": "edge",
            "sharding_key": "rand()",
        },
    }
    configure(
        aliases=("source", "target"),
        driver=transport,
        transfer_staging_schema="db",
        per_alias={
            "target": {
                "cluster_routing": None,
                "ddl_defaults": {"regular": deepcopy(scope), "staging": deepcopy(scope)},
            }
        },
    )
    hosts = ("h1", "h3") if partial else ("h3", "h4")
    cluster.members_by_cluster["edge"] = [
        (index, 1, host, 9000) for index, host in enumerate(hosts, 1)
    ]
    return cluster


def observe_created_resources(monkeypatch: Any) -> tuple[list[Any], list[Any]]:
    captured, dropped = [], []
    capture = created_resources.capture_created_resource
    drop = created_resources.drop_created_resource

    def capture_resource(*args: Any, **kwargs: Any) -> Any:
        resource = capture(*args, **kwargs)
        captured.append(resource)
        return resource

    def drop_resource(*args: Any, **kwargs: Any) -> Any:
        result = drop(*args, **kwargs)
        dropped.append(args[2])
        return result

    monkeypatch.setattr(created_resources, "capture_created_resource", capture_resource)
    monkeypatch.setattr(created_resources, "drop_created_resource", drop_resource)
    return captured, dropped
