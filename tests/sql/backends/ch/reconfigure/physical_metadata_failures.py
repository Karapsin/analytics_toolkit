"""Malformed or changed physical metadata cannot certify a live rebuild."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.observation import observe_sql

from tests.sql._support.reconfigure_independent_replicas import ReconfigureSubmissionLog
from tests.sql._support.reconfigure_physical_metadata_failures import (
    install_http_metadata,
    install_metadata_fault,
)
from tests.sql._support.reconfigure_physical_scope import reconfigure_physical


def _assert_complete_initial_proof(raw):
    assert raw.complete_memberships
    assert all(
        len(rows) == 2 and all(len(row) == 6 for row in rows) for rows in raw.complete_memberships
    )
    assert raw.complete_identity_probes
    assert all({row[0] for row in rows} == {1, 2} for rows in raw.complete_identity_probes)
    assert isinstance(raw.wrappers[0], OutageClient)
    assert raw.wrappers[0]._client._sql_observed is True


@pytest.mark.parametrize(
    "members",
    [
        [(1, 1, "coordinator")],
        [(1, 1, "coordinator"), (1, 2, "remote"), (2, 1, "unexpected")],
        [(1, 1, "coordinator"), (1, 2)],
        [(1, 1, "coordinator"), (1, 2, "remote", "unexpected")],
        [(1, 1, "coordinator"), ("not-an-integer", 2, "remote")],
        [(1, 1, "coordinator"), (None, 2, "remote")],
        [(0, 1, "coordinator"), (1, 2, "remote")],
        [(1, 1, "coordinator"), (1, 0, "remote")],
        [(1, 1, "coordinator"), (1, 1, "remote")],
        [(1, 1, "coordinator"), (1, 2, "")],
    ],
    ids=[
        "missing-member",
        "extra-member",
        "short-row",
        "wide-row",
        "invalid-integer",
        "null-integer",
        "nonpositive-shard",
        "nonpositive-replica",
        "duplicate",
        "empty-host",
    ],
)
def test_physical_second_membership_read_rejects_malformed_metadata_before_ddl(
    monkeypatch, write_sql_connections, members
):
    raw = install_metadata_fault(monkeypatch, write_sql_connections, member_rows=members)
    original = raw.snapshot()
    submissions = ReconfigureSubmissionLog()

    with observe_sql(submissions), pytest.raises(
        InvalidSqlInputError, match="physical source endpoint membership"
    ):
        reconfigure_physical(dry_run=True)

    _assert_complete_initial_proof(raw)
    assert raw.membership_faults == [[(1, 1, "coordinator"), (1, 2, "remote")]]
    assert raw.snapshot() == original
    assert raw.commands == raw.worker_connections == raw.worker_commands == []
    assert raw.counts == []
    assert raw.closed


def test_physical_strict_source_certificate_rejects_ddl_changed_after_initial_selection(
    monkeypatch, write_sql_connections
):
    raw = install_metadata_fault(monkeypatch, write_sql_connections, change_ddl=True)
    original_rows = raw.physical_counters()
    submissions = ReconfigureSubmissionLog()

    with observe_sql(submissions), pytest.raises(
        InvalidSqlInputError, match="Physical source DDL differs"
    ):
        reconfigure_physical(dry_run=True)

    _assert_complete_initial_proof(raw)
    assert len(raw.weak_source_definitions) == 2
    assert raw.changed_snapshot is not None
    assert raw.snapshot() == raw.changed_snapshot
    assert raw.physical_counters() == original_rows
    assert raw.commands == raw.worker_connections == raw.worker_commands == []
    assert raw.counts == []
    assert raw.closed


def test_physical_http_cannot_address_same_hostname_with_distinct_native_tcp_ports(
    monkeypatch, write_sql_connections
):
    raw = install_http_metadata(monkeypatch, write_sql_connections, shared_hostname=True)
    original = raw.snapshot()
    config = get_connection_config("ch")
    assert config.driver == "http"
    assert config.port == 8123
    submissions = ReconfigureSubmissionLog()

    with observe_sql(submissions), pytest.raises(
        InvalidSqlInputError, match="HTTP endpoint cannot distinguish"
    ):
        reconfigure_physical(dry_run=True)

    _assert_complete_initial_proof(raw)
    assert raw.complete_memberships[0] == [
        (1, 1, "shared-host", "10.0.0.1", 9000, 1),
        (1, 2, "shared-host", "10.0.0.2", 9001, 0),
    ]
    assert raw.snapshot() == original
    assert raw.commands == raw.http_worker_opens == raw.worker_connections == []
    assert raw.closed


def test_physical_distinct_http_endpoints_preserve_rows_and_keep_configured_http_port(
    monkeypatch, write_sql_connections
):
    raw = install_http_metadata(monkeypatch, write_sql_connections)
    original = raw.physical_counters()
    submissions = ReconfigureSubmissionLog()

    with observe_sql(submissions):
        result = reconfigure_physical()

    _assert_complete_initial_proof(raw)
    assert raw.physical_counters() == original
    assert result.metadata.source_rows == result.metadata.staged_rows == 103
    assert result.metadata.final_target_rows == 103
    assert result.metadata.row_count_validated is True
    assert result.data["cleanup_complete"] is True
    assert raw.http_worker_opens == [(raw.remote, 8123, 9000)]
    assert raw.worker_connections
    assert all(worker.closed for worker in raw.worker_connections)
    assert raw.closed
