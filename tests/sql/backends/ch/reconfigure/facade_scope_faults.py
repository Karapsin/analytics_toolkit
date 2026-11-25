"""Incomplete facade evidence cannot authorize a public table reconfiguration."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql._support.reconfigure_facade_faults import (
    assert_rejected_unchanged,
    mutate_facade,
    prepare_fault,
    projection,
    query_identity,
)

_MEMBERSHIP = (
    (None, "system", "clusters"),
    ("shard_num", "replica_num", "host_name", "host_address", "port", "is_local"),
)
_LOCAL_IDENTITY = ((None, None, None), ("serveruuid", "tcpport"))
_REMOTE_IDENTITIES = (("core", "system", "one"), ("_shard_num", "serveruuid", "tcpport"))
_DEFINITIONS = (("core", "system", "tables"), ("_shard_num", "create_table_query"))
_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _with_fields(rows, position, fields):
    row = list(rows[position])
    for index, value in fields.items():
        row[index] = value
    return [*rows[:position], tuple(row), *rows[position + 1 :]]


def _check_public_rejection(
    monkeypatch, write_sql_connections, target, replace_rows, *, dry_run=False
):
    def transform(select, rows):
        if (query_identity(select), projection(select)) == target:
            return replace_rows(rows)
        return None

    raw = prepare_fault(monkeypatch, write_sql_connections, transform)
    before = raw.snapshot()

    with pytest.raises(InvalidSqlInputError, match=r"[Ff]acade"):
        mutate_facade(dry_run=dry_run)

    assert raw.fault_hits == [target]
    assert_rejected_unchanged(raw, before)


@pytest.mark.parametrize("dry_run", [True, False], ids=["plan", "execute"])
@pytest.mark.parametrize(
    "replace_rows",
    [
        pytest.param(lambda rows: [], id="no-configured-members"),
        pytest.param(lambda rows: [rows[0][:-1], *rows[1:]], id="truncated-membership"),
        pytest.param(
            lambda rows: _with_fields(rows, 0, {0: "unknown-shard"}),
            id="unparseable-shard",
        ),
        pytest.param(lambda rows: _with_fields(rows, 0, {0: 0}), id="zero-shard-ordinal"),
        pytest.param(lambda rows: _with_fields(rows, 0, {1: 0}), id="zero-replica-ordinal"),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {0: rows[0][0], 1: rows[0][1]}),
            id="duplicate-membership-position",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {2: rows[0][2], 3: rows[0][3], 4: rows[0][4]}),
            id="duplicate-native-endpoint",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {2: "", 3: ""}),
            id="missing-native-address",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {4: "unknown-port"}),
            id="unparseable-native-port",
        ),
        pytest.param(lambda rows: _with_fields(rows, 1, {4: 0}), id="zero-native-port"),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {4: 65536}),
            id="native-port-out-of-range",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {5: 2}),
            id="nonboolean-locality-flag",
        ),
    ],
)
def test_public_facade_change_requires_valid_configured_native_membership(
    monkeypatch, write_sql_connections, replace_rows, dry_run
):
    _check_public_rejection(
        monkeypatch,
        write_sql_connections,
        _MEMBERSHIP,
        replace_rows,
        dry_run=dry_run,
    )


@pytest.mark.parametrize(
    "replace_rows",
    [
        pytest.param(lambda rows: [], id="missing-coordinator-identity"),
        pytest.param(lambda rows: [rows[0], rows[0]], id="ambiguous-coordinator-identity"),
        pytest.param(lambda rows: [rows[0][:1]], id="truncated-coordinator-identity"),
        pytest.param(
            lambda rows: _with_fields(rows, 0, {0: "unknown-server"}),
            id="invalid-coordinator-uuid",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 0, {0: _NIL_UUID}),
            id="nil-coordinator-uuid",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 0, {1: None}),
            id="missing-coordinator-port",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 0, {1: -1}),
            id="negative-coordinator-port",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 0, {1: 65536}),
            id="coordinator-port-out-of-range",
        ),
    ],
)
def test_public_facade_change_requires_one_valid_coordinator_identity(
    monkeypatch, write_sql_connections, replace_rows
):
    _check_public_rejection(monkeypatch, write_sql_connections, _LOCAL_IDENTITY, replace_rows)


@pytest.mark.parametrize(
    "replace_rows",
    [
        pytest.param(lambda rows: [], id="no-reached-servers"),
        pytest.param(lambda rows: rows[:-1], id="unreached-configured-server"),
        pytest.param(lambda rows: [rows[0][:-1], *rows[1:]], id="truncated-remote-identity"),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {0: "unknown-position"}),
            id="unparseable-remote-position",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {1: "unknown-server"}),
            id="invalid-remote-uuid",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {1: _NIL_UUID}),
            id="nil-remote-uuid",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {2: "unknown-port"}),
            id="unparseable-remote-port",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {0: rows[0][0]}),
            id="duplicate-remote-position",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {0: len(rows) + 1}),
            id="unexpected-remote-position",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {1: rows[0][1], 2: rows[0][2]}),
            id="duplicate-server-identity",
        ),
        pytest.param(
            lambda rows: [
                (rows[0][0], rows[1][1], rows[1][2]),
                (rows[1][0], rows[0][1], rows[0][2]),
            ],
            id="coordinator-resolves-to-different-server",
        ),
    ],
)
def test_public_facade_change_requires_complete_distinct_remote_identities(
    monkeypatch, write_sql_connections, replace_rows
):
    _check_public_rejection(monkeypatch, write_sql_connections, _REMOTE_IDENTITIES, replace_rows)


@pytest.mark.parametrize("dry_run", [True, False], ids=["plan", "execute"])
@pytest.mark.parametrize(
    "replace_rows",
    [
        pytest.param(lambda rows: [], id="all-logical-facades-missing"),
        pytest.param(lambda rows: [rows[0][:-1], *rows[1:]], id="truncated-definition"),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {1: None}),
            id="missing-create-definition",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {0: "unknown-position"}),
            id="unparseable-definition-position",
        ),
        pytest.param(lambda rows: [rows[0], rows[0]], id="duplicate-definition-position"),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {0: len(rows) + 1}),
            id="unconfigured-definition-position",
        ),
        pytest.param(
            lambda rows: _with_fields(rows, 1, {1: ""}),
            id="empty-create-definition",
        ),
    ],
)
def test_public_facade_change_requires_complete_unambiguous_create_definitions(
    monkeypatch, write_sql_connections, replace_rows, dry_run
):
    _check_public_rejection(
        monkeypatch,
        write_sql_connections,
        _DEFINITIONS,
        replace_rows,
        dry_run=dry_run,
    )
