"""Physical column encodings cannot change logical facade/shard identity."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.layout import (
    _column_identity,
    _inspect_existing_layout_local,
    inspect_existing_layout,
)
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.backends.ch.routing import local_sql
from sqlglot import exp, parse_one

from tests.sql._support.ch_partition_scope import install_partition_scope

_COLUMNS = (
    "`source` LowCardinality(String)",
    "`country` LowCardinality(String)",
    "`p` String DEFAULT 'CODEC(ZSTD(1))'",
    "`id` UInt64 DEFAULT 7",
)


def _definition(columns):
    return parse_create_table(
        "CREATE TABLE analytics.events (" + ", ".join(columns) + ") "
        "ENGINE=MergeTree ORDER BY tuple()",
        "analytics.events",
    )


@pytest.mark.parametrize("codec", ["ZSTD(1)", "ZSTD(9)", "LZ4"])
def test_column_identity_ignores_codec_on_every_column_without_mutating_ddl(codec):
    logical = _definition(_COLUMNS)
    physical = _definition([column + f" CODEC({codec})" for column in _COLUMNS])
    before = physical.sql(dialect=ClickHouseDialect)

    assert _column_identity(logical) == _column_identity(physical)

    assert physical.sql(dialect=ClickHouseDialect) == before
    assert all(
        any(isinstance(item.kind, exp.CompressColumnConstraint) for item in column.constraints)
        for column in physical.this.expressions
    )


@pytest.mark.parametrize(
    "columns",
    [
        ("`renamed` LowCardinality(String)", *_COLUMNS[1:]),
        (_COLUMNS[1], _COLUMNS[0], *_COLUMNS[2:]),
        (*_COLUMNS[:3], "`id` UInt32 DEFAULT 7"),
        (*_COLUMNS[:3], "`id` UInt64 DEFAULT 8"),
        (*_COLUMNS[:2], "`p` String DEFAULT 'different'", _COLUMNS[3]),
    ],
    ids=["name", "order", "type", "numeric-default", "string-default"],
)
def test_ignoring_codec_does_not_hide_logical_column_differences(columns):
    logical = _definition(_COLUMNS)
    physical = _definition([column + " CODEC(ZSTD(1))" for column in columns])

    assert _column_identity(logical) != _column_identity(physical)


def test_column_identity_ignores_codec_and_existing_ttl_together():
    logical = _definition(["expires DateTime DEFAULT now()"])
    physical = _definition(
        ["expires DateTime DEFAULT now() CODEC(ZSTD(1)) TTL expires + INTERVAL 1 DAY"]
    )

    assert _column_identity(logical) == _column_identity(physical)


@pytest.fixture(params=["http", "native"])
def codec_pair(monkeypatch, write_sql_connections, request):
    state = install_partition_scope(
        monkeypatch, write_sql_connections, driver=request.param, layout="pair"
    )
    physical = state.host_tables[state.remote]["analytics.events_shard"]
    physical.create = parse_create_table(
        "CREATE TABLE analytics.events_shard "
        "(p String CODEC(ZSTD(1)), id UInt64 CODEC(ZSTD(1))) "
        "ENGINE=MergeTree PARTITION BY p ORDER BY id",
        "analytics.events_shard",
    )
    return state


def test_both_layout_inspectors_recognize_codec_only_managed_pair(codec_pair):
    state = codec_pair
    before = state.snapshot()
    connection = sql.get_sql_connection("ch")
    try:
        with local_sql(connection):
            direct = _inspect_existing_layout_local(connection, "analytics.events")
        wrapped = inspect_existing_layout(connection, "analytics.events")
        assert direct.owns_physical is True
        assert wrapped.owns_physical is True
        assert direct.physical_table == wrapped.physical_table
        physical = parse_one(direct.physical_table, read=ClickHouseDialect, into=exp.Table)
        assert (physical.db, physical.name) == ("analytics", "events_shard")
    finally:
        connection.close()
    assert state.snapshot() == before
    assert state.commands == []
    assert all(client.closed and client.close_calls == 1 for client in state.opened)


def test_public_drop_partitions_accepts_codec_only_managed_pair(codec_pair):
    state = codec_pair
    expected = deepcopy(state.snapshot())
    expected[state.remote]["analytics.events_shard"]["rows"] = [("kept", 2)]

    assert (
        sql.drop_partitions("ch", "analytics.events", ["old"], retry_cnt=1, timeout_increment=0)
        is None
    )

    assert state.snapshot() == expected
    assert len(state.commands) == 1
    assert all(client.closed and client.close_calls == 1 for client in state.opened)
