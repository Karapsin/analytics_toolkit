"""Reconfiguration must preserve independently stored rows on every replica."""

from __future__ import annotations

from collections import Counter

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.execution.observation import observe_sql

from tests.sql._support.reconfigure_independent_replicas import (
    IndependentReplicaClient,
    ReconfigureSubmissionLog,
    configure_independent_replicas,
    connect_independent_replicas,
)


@pytest.mark.parametrize("internal_replication", [True, False], ids=["one-target", "all-targets"])
@pytest.mark.parametrize(
    "replicas", [1, 2], ids=["single-physical-replica", "independent-replicas"]
)
def test_independent_replica_reconfiguration_preserves_every_original_physical_record(
    monkeypatch, write_sql_connections, internal_replication, replicas
):
    configure_independent_replicas(write_sql_connections)
    config = get_connection_config("ch")
    assert config.cluster_routing is None
    assert config.settings["max_parallel_replicas"] == 1
    raw = IndependentReplicaClient(internal_replication=internal_replication, replicas=replicas)
    original = raw.physical_counters()
    expected_total = Counter()
    for rows in original.values():
        expected_total.update(rows)
    assert sum(expected_total.values()) == (3 if replicas == 1 else 103)
    facade_uuid = raw.host_tables[raw.coordinator][raw.source_table].uuid
    submissions = ReconfigureSubmissionLog()
    connect_independent_replicas(monkeypatch, raw)

    with observe_sql(submissions):
        result = sql.ch_reconfigure_table(
            "ch",
            "analytics.events",
            order_by="cityHash64(id)",
            retry_cnt=1,
            timeout_increment=0,
            return_metadata=True,
        )

    assert len(raw.wrappers) == 1
    assert isinstance(raw.wrappers[0], OutageClient)
    assert raw.wrappers[0]._client._sql_observed is True
    assert submissions.sql
    assert result.data["strategy"] == "managed_pair_rebuild"
    assert result.metadata.row_count_validated is True
    assert result.data["cleanup_complete"] is True, result.data["cleanup_error"]
    assert raw.host_tables[raw.coordinator][raw.source_table].uuid == facade_uuid
    actual = raw.physical_counters()
    actual_total = Counter()
    for rows in actual.values():
        actual_total.update(rows)
    diagnostic = {
        "internal_replication": internal_replication,
        "counts": (
            result.metadata.source_rows,
            result.metadata.staged_rows,
            result.metadata.final_target_rows,
        ),
        "validated": result.metadata.row_count_validated,
        "native_read_selection": raw.native_reads,
        "native_insert_fanout": raw.native_insert_fanout,
        "original_by_host": original,
        "actual_by_host": actual,
        "commands": raw.commands,
    }
    assert actual_total == expected_total, repr(diagnostic)
    assert actual == original, repr(diagnostic)
    assert raw.closed
