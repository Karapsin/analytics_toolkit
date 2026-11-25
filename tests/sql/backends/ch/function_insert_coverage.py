"""Explicit cluster insert functions retain physical or logical coverage intent."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql


def _replicated_one_shard(outage: Any) -> Any:
    cluster, configure = outage
    configure(cluster_routing=None)
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    cluster.seed(replicated=True)
    return cluster


@pytest.mark.parametrize("function", ["cluster", "clusterAllReplicas"])
def test_public_insert_accepts_static_two_argument_cluster_target(
    outage: Any, function: str
) -> None:
    cluster = _replicated_one_shard(outage)
    sql.execute(
        "ch",
        f"INSERT INTO FUNCTION {function}('core','db.events_shard') VALUES (3)",
        retry_cnt=1,
    )
    assert any(
        "INSERT" in command[1] and "events_shard" in command[1] for command in cluster.commands
    )


def test_explicit_all_replica_insert_requires_each_host_without_automatic_routing(
    outage: Any,
) -> None:
    cluster = _replicated_one_shard(outage)
    cluster.alive.remove("h2")
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.execute(
            "ch",
            "INSERT INTO FUNCTION clusterAllReplicas('core','db','events_shard') VALUES (3)",
            retry_cnt=1,
        )
    assert not any("INSERT" in command[1] for command in cluster.commands)


@pytest.mark.parametrize(
    "identity",
    [
        "db.events_shard AS other",
        "db.events_shard; db.other",
        "db.events_shard FINAL",
        "db.events_shard /* suffix */",
    ],
)
def test_static_function_identity_rejects_discarded_sql_fragments_before_insert(
    outage: Any, identity: str
) -> None:
    cluster = _replicated_one_shard(outage)
    with pytest.raises(sql.ClickHouseClusterTopologyError, match=r"static|identifier"):
        sql.execute(
            "ch",
            f"INSERT INTO FUNCTION cluster('core','{identity}') VALUES (3)",
            retry_cnt=1,
        )
    assert not any("INSERT" in command[1] for command in cluster.commands)


def test_static_identifier_argument_comments_do_not_change_identity(outage: Any) -> None:
    cluster = _replicated_one_shard(outage)
    sql.execute(
        "ch",
        "INSERT INTO FUNCTION cluster('core', db.events_shard /* outer comment */) VALUES (3)",
        retry_cnt=1,
    )
    assert any(
        "INSERT" in command[1] and "'events_shard'" in command[1] for command in cluster.commands
    )
