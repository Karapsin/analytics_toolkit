"""Physical copy classification requires one coherent membership observation."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import (
    ClickHouseClusterTopologyError,
    InvalidSqlInputError,
)
from analytics_toolkit.sql.execution.observation import observe_sql
from analytics_toolkit.sql.execution.plans import SqlPlan

from tests.sql._support.reconfigure_independent_replicas import ReconfigureSubmissionLog
from tests.sql._support.reconfigure_physical_metadata_failures import install_metadata_fault
from tests.sql._support.reconfigure_physical_scope import reconfigure_physical


def _assert_truthful_endpoint_proof(raw, original):
    assert isinstance(raw.wrappers[0], OutageClient)
    assert raw.wrappers[0]._client._sql_observed is True
    assert raw.complete_memberships
    assert all(
        [(row[0], row[1], row[2]) for row in rows] == [(1, 1, "coordinator"), (1, 2, "remote")]
        for rows in raw.complete_memberships
    )
    assert raw.complete_identity_probes
    assert all({row[0] for row in rows} == {1, 2} for rows in raw.complete_identity_probes)
    assert raw.membership_faults
    assert all(rows == [(1, 1, "coordinator"), (1, 2, "remote")] for rows in raw.membership_faults)
    assert raw.snapshot() == original
    assert raw.counts == raw.native_reads == raw.native_insert_fanout == []
    assert raw.commands == raw.worker_connections == raw.worker_commands == []
    assert raw.closed


def test_physical_membership_grouping_change_cannot_reclassify_source_before_dry_plan(
    monkeypatch, write_sql_connections
):
    changed_members = [(1, 1, "coordinator"), (2, 1, "remote")]
    raw = install_metadata_fault(monkeypatch, write_sql_connections, member_rows=changed_members)
    assert get_connection_config("ch").cluster_routing is None
    original = raw.snapshot()
    submissions = ReconfigureSubmissionLog()

    with observe_sql(submissions):
        try:
            with pytest.raises(
                (InvalidSqlInputError, ClickHouseClusterTopologyError),
                match=r"(?i)(membership|changed|inconsistent|scope|config|physical)",
            ):
                reconfigure_physical(dry_run=True)
        finally:
            _assert_truthful_endpoint_proof(raw, original)


def test_physical_membership_stable_grouping_keeps_endpoint_local_dry_plan(
    monkeypatch, write_sql_connections
):
    stable_members = [(1, 1, "coordinator"), (1, 2, "remote")]
    raw = install_metadata_fault(monkeypatch, write_sql_connections, member_rows=stable_members)
    original = raw.snapshot()
    submissions = ReconfigureSubmissionLog()

    with observe_sql(submissions):
        plan = reconfigure_physical(dry_run=True)

    assert isinstance(plan, SqlPlan)
    assert plan.options["physical_copy_mode"] == "endpoint_local"
    assert len(plan.options["endpoint_scoped_sql"]) == 2
    _assert_truthful_endpoint_proof(raw, original)
