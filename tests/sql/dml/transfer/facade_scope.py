"""Replacement must retain the verified execution scope of every original facade."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_statements import exchange_or_rename
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_transfer_replacement import ReplacementScopeState
from tests.sql._support.outage_gp import Connection, MemoryGreenplum

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"


def _source_values(shape):
    if shape == "type":
        return [(201,), (202,)], {"id": "UInt32"}
    return [(201, 1201), (202, 1202)], {"id": "UInt64", "payload": "UInt64"}


def _install_two_facades(monkeypatch, write_sql_connections, shape, desired_facade):
    connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
    connections["ch"].update(
        driver="http",
        host="coordinator",
        database="analytics",
        transfer_staging_schema="analytics",
        cluster_routing={"cluster": "core"},
        ddl_defaults={
            "regular": {
                "create_distributed_pair": True,
                "shard": {"engine": "MergeTree", "on_cluster": "storage"},
                "distributed": {
                    "engine_template": (
                        "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                    ),
                    "cluster": "storage",
                    "on_cluster": desired_facade,
                    "sharding_key": "rand()",
                },
            },
            "staging": {
                "create_distributed_pair": False,
                "shard": {"engine": "MergeTree", "on_cluster": "core"},
            },
        },
    )
    set_connections_path(write_sql_connections(connections))
    rows, schema = _source_values(shape)
    source = MemoryGreenplum()
    source.tables["db.source"] = pd.DataFrame(rows, columns=list(schema))
    state = ReplacementScopeState()
    state.clusters["core_alias"] = state.clusters["core"]
    state._create(state.remote, state.host_tables[state.coordinator][_TARGET].create.copy())
    monkeypatch.setattr(
        get_backend_adapter("gp"), "open_connection", lambda *_args, **_kwargs: Connection(source)
    )
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", state.open)
    return source, state, rows, schema


def _replace(schema, desired_facade):
    source_sql = (
        "SELECT id FROM db.source" if len(schema) == 1 else "SELECT id, payload FROM db.source"
    )
    return sql.transfer(
        from_db="gp",
        to_db="ch",
        from_sql=source_sql,
        to_table=_TARGET,
        write_mode="replace",
        table_schema=schema,
        ignore_source_staging=True,
        batch_size=2,
        min_batch_size=1,
        max_batch_size=2,
        adaptive_batch_size=False,
        retry_cnt=1,
        full_retry_cnt=1,
        timeout_increment=0,
        full_timeout_increment=0,
        return_metadata=True,
        ch_distributed_table=True,
        ch_engine="MergeTree",
        ch_shard_on_cluster="storage",
        ch_distributed_cluster="storage",
        ch_distributed_on_cluster=desired_facade,
        ch_sharding_key="rand()",
        order_by=["id"],
    )


def _replacement_that_must_reject(state, before, schema, desired_facade):
    try:
        result = _replace(schema, desired_facade)
    except Exception as error:
        diagnostic = {
            "error": repr(error),
            "before": before,
            "after": state.snapshot(),
            "commands": state.commands,
        }
        assert state.snapshot() == before, repr(diagnostic)
        raise
    diagnostic = {
        "counts": (
            result.metadata.source_rows,
            result.metadata.staged_rows,
            result.metadata.final_target_rows,
        ),
        "before": before,
        "after": state.snapshot(),
        "commands": state.commands,
    }
    assert state.snapshot() == before, repr(diagnostic)
    pytest.fail("Staged replacement accepted a strict subset of the original facade scope")


def _assert_no_original_cutover(state):
    for statement in state.commands:
        body = strip_query_label(statement)
        special = exchange_or_rename(body)
        if special is not None:
            operation, _scopes = special
            assert all(
                table.name not in {"events", "events_shard"} for table in operation.expressions
            )
        elif body.startswith("CREATE TABLE"):
            create = parse_one(body, read=ClickHouseDialect)
            target = create.this.this if isinstance(create.this, exp.Schema) else create.this
            assert "__replace_guarded_" not in target.name


@pytest.mark.parametrize("shape", ["type", "column"])
@pytest.mark.parametrize("desired_facade", ["coordinator_only", None], ids=["subset", "local"])
def test_public_transfer_rejects_replacement_that_leaves_an_original_peer_facade(
    monkeypatch, write_sql_connections, shape, desired_facade
):
    source, state, rows, schema = _install_two_facades(
        monkeypatch, write_sql_connections, shape, desired_facade
    )
    before = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match=r"[Ff]acade|[Ss]cope|[Pp]lacement"):
        _replacement_that_must_reject(state, before, schema, desired_facade)

    assert state.snapshot() == before
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.rows(state.remote, _TARGET) == [(11,), (12,), (13,)]
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == rows
    _assert_no_original_cutover(state)
    assert state.opened
    assert all(connection.closed for connection in state.opened)


@pytest.mark.parametrize("shape", ["type", "column"])
def test_public_transfer_accepts_an_equivalent_complete_facade_scope_alias(
    monkeypatch, write_sql_connections, shape
):
    source, state, rows, schema = _install_two_facades(
        monkeypatch, write_sql_connections, shape, "core_alias"
    )
    before = state.snapshot()

    result = _replace(schema, "core_alias")

    assert result.rows == 2
    assert result.data is None
    assert result.metadata.source_rows == result.metadata.staged_rows == 2
    assert result.metadata.final_target_rows == 2
    assert result.metadata.row_count_validated is True
    after = state.snapshot()
    assert after[state.coordinator][_PHYSICAL] == before[state.coordinator][_PHYSICAL]
    assert state.rows(state.remote, _PHYSICAL) == rows
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    for host in (state.coordinator, state.remote):
        assert state.rows(host, _TARGET) == rows
        assert state.host_tables[host][_TARGET].schema == schema
        assert after[host][_TARGET]["uuid"] != before[host][_TARGET]["uuid"]
        assert set(after[host]) == set(before[host])
    assert after[state.remote][_PHYSICAL]["uuid"] != before[state.remote][_PHYSICAL]["uuid"]
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == rows
    assert state.opened
    assert all(connection.closed for connection in state.opened)
