"""Same-topology replacement certifies complete remote facade ownership."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.ch_transfer_replacement import ReplacementScopeState
from tests.sql._support.outage_gp import MemoryGreenplum

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"
_SOURCE_ROWS = [(201,), (202,)]


def _install(monkeypatch, write_sql_connections, transport, *, foreign=False, metadata_fault=None):
    connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
    connections["ch"].update(
        driver=transport,
        host="coordinator",
        port=9000 if transport == "native" else 8123,
        database="analytics",
        transfer_staging_schema="analytics",
        ddl_defaults={
            "regular": {
                "create_distributed_pair": True,
                "shard": {"engine": "MergeTree", "on_cluster": "storage"},
                "distributed": {
                    "engine_template": (
                        "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                    ),
                    "cluster": "storage",
                    "on_cluster": "frontends",
                    "sharding_key": "rand()",
                },
            },
            "staging": {
                "create_distributed_pair": False,
                "shard": {"engine": "Memory", "on_cluster": None},
            },
        },
    )
    set_connections_path(write_sql_connections(connections))
    state = ReplacementScopeState()
    facade = state.host_tables[state.coordinator][_TARGET].create.copy()
    rows = []
    if foreign:
        facade = parse_create_table(
            "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY id", _TARGET
        )
        rows = [(value,) for value in range(501, 601)]
    state._create(state.remote, facade, rows)
    state.created.clear()
    if metadata_fault is not None:

        def metadata_transform(tree, _rows):
            source = tree.args.get("from_")
            if source is None:
                return None
            target = source.this
            if isinstance(target.this, exp.Anonymous):
                cluster, database, relation, _extra = cluster_arguments(target.this, "analytics")
                if metadata_fault == "incomplete" and (cluster, database, relation) == (
                    "frontends",
                    "system",
                    "one",
                ):
                    return []
                if metadata_fault == "grouping" and (cluster, database, relation) == (
                    "frontends",
                    "system",
                    "clusters",
                ):
                    return [(row[0], row[1], 2, *row[3:]) for row in _rows]
            return None

        state.metadata_transform = metadata_transform
    source = MemoryGreenplum()
    source.tables["db.source"] = pd.DataFrame(_SOURCE_ROWS, columns=["id"])
    source.install(monkeypatch)
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", state.open)
    return source, state


def _transfer():
    return sql.transfer(
        from_db="gp",
        to_db="ch",
        from_sql="SELECT id FROM db.source",
        to_table=_TARGET,
        write_mode="replace",
        table_schema={"id": "UInt64"},
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
        ch_distributed_on_cluster="frontends",
        ch_sharding_key="rand()",
        order_by=["id"],
    )


@pytest.mark.parametrize("transport", ["http", "native"])
def test_public_transfer_replaces_verified_remote_facades_without_a_coordinator_member(
    monkeypatch, write_sql_connections, transport
):
    source, state = _install(monkeypatch, write_sql_connections, transport)
    before = state.snapshot()

    result = _transfer()

    assert result.rows == 2
    assert result.data is None
    assert result.metadata.source_rows == 2
    assert result.metadata.staged_rows == 2
    assert result.metadata.final_target_rows == 2
    assert result.metadata.row_count_validated is True
    assert state.rows(state.remote, _PHYSICAL) == _SOURCE_ROWS
    assert state.rows(state.remote, _TARGET) == _SOURCE_ROWS
    assert state.rows(state.coordinator, _TARGET) == _SOURCE_ROWS
    after = state.snapshot()
    assert after[state.coordinator] == before[state.coordinator]
    assert set(after[state.remote]) == set(before[state.remote])
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == _SOURCE_ROWS
    assert state.inserts
    assert state.opened
    assert all(connection.closed for connection in state.opened)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("fault", ["foreign", "incomplete", "grouping"])
def test_public_transfer_remote_facade_scope_still_requires_complete_owned_endpoints(
    monkeypatch, write_sql_connections, transport, fault
):
    source, state = _install(
        monkeypatch,
        write_sql_connections,
        transport,
        foreign=fault == "foreign",
        metadata_fault=None if fault == "foreign" else fault,
    )
    before = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match=r"facade|Facade|coordinator"):
        _transfer()

    assert state.snapshot() == before
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == _SOURCE_ROWS
    assert not any("__replace_guarded_" in statement for statement in state.commands)
    assert state.opened
    assert all(connection.closed for connection in state.opened)


@pytest.mark.parametrize("transport", ["http", "native"])
def test_public_reconfiguration_keeps_its_coordinator_facade_requirement(
    monkeypatch, write_sql_connections, transport
):
    _source, state = _install(monkeypatch, write_sql_connections, transport)
    before = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match="coordinator"):
        sql.ch_reconfigure_table(
            "ch",
            _TARGET,
            ch_distributed_table=True,
            ch_engine="MergeTree",
            ch_shard_on_cluster="storage",
            ch_distributed_cluster="storage",
            ch_distributed_on_cluster="frontends",
            ch_sharding_key="id",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert state.snapshot() == before
    assert state.commands == []
    assert state.opened
    assert all(connection.closed for connection in state.opened)
