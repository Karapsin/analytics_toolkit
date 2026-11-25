"""Metadata fixtures expose the actual one-replica versus all-replica contract."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql

from tests.sql._support.reconfigure import ReconfigureClient


@pytest.mark.parametrize(("function", "expected"), [("cluster", 2), ("clusterAllReplicas", 4)])
def test_public_metadata_reads_distinguish_logical_and_physical_replica_origins(
    outage: Any, function: str, expected: int
) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    result = sql.read(
        "ch",
        f"SELECT name FROM {function}('core',system,tables)",
        retry_cnt=1,
    )
    assert len(result) == expected


@pytest.mark.parametrize(("function", "expected"), [("cluster", 1), ("clusterAllReplicas", 2)])
def test_reconfiguration_metadata_fixture_distinguishes_replica_origins(
    function: str, expected: int
) -> None:
    client = ReconfigureClient(
        source_hosts=(("one", "10.0.0.1", 9000), ("two", "10.0.0.2", 9000)),
    )
    rows = client.metadata.rows(f"SELECT hostName() FROM {function}('core',system,one)")
    assert len(rows) == expected
