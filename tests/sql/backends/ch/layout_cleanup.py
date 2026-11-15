"""Cleanup authorizes resources by verified layout rather than naming guesses."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch import lifecycle, operations
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.ch_drop_scope import install_drop_scope


def test_missing_table_cleanup_retains_requested_cluster_scope(
    monkeypatch: Any, write_sql_connections: Any
) -> None:
    state = install_drop_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()
    sql.drop_tables("ch", "analytics.absent", if_exists=True, ch_cluster="storage")
    # Verified absence authorizes no resources, including guessed local mirrors.
    assert state.snapshot() == before
    assert state.commands == []
    assert state.query_events
    assert all(client.closed and client.close_calls == 1 for client in state.opened)


@pytest.mark.parametrize("remove_facade", [True, False])
def test_external_storage_is_never_owned_by_its_facade(outage: Any, remove_facade: bool) -> None:
    cluster, _configure = outage
    for host in cluster.hosts:
        cluster.create(
            host, "CREATE TABLE db.external_storage (id UInt64) ENGINE=MergeTree ORDER BY id"
        )
        cluster.tables[(host, "db.external_storage")].rows = [(index,) for index in range(100)]
        cluster.create(
            host,
            "CREATE TABLE db.remote_facade (id UInt64) "
            "ENGINE=Distributed('core','db','external_storage',rand())",
        )
    if remove_facade:
        sql.drop_tables("ch", "db.remote_facade", ch_cluster="core", if_exists=True)
        assert not any(name == "db.remote_facade" for _host, name in cluster.tables)
    else:
        with pytest.raises(InvalidSqlInputError, match="named explicitly"):
            sql.drop_tables(
                "ch",
                "db.remote_facade",
                ch_cluster="core",
                ch_drop_distributed=False,
                if_exists=True,
            )
        assert all((host, "db.remote_facade") in cluster.tables for host in cluster.hosts)
    assert all(
        len(cluster.tables[(host, "db.external_storage")].rows) == 100 for host in cluster.hosts
    )


def test_external_facade_cleanup_sql_never_includes_storage() -> None:
    policy = SimpleNamespace(
        create_distributed_pair=True,
        owns_physical=False,
        shard_on_cluster="storage",
        distributed_on_cluster="facades",
        physical_table="db.external_storage",
    )
    commands = lifecycle.build_drop_ch_creation_policy_table_sqls("db.remote_facade", policy)
    targets = [parse_one(command, read="clickhouse").find(exp.Table).name for command in commands]
    assert targets == ["remote_facade"]
    assert "facades" in commands[0]
    assert all("external_storage" not in command for command in commands)


def test_prepare_missing_existing_target_skips_cleanup_and_population() -> None:
    calls = []
    adapter = SimpleNamespace(
        table_exists=lambda *_args, **_kwargs: calls.append("exists") or False
    )
    assert (
        operations.prepare_existing_target_for_create_from_sql(
            adapter, object(), "db.absent", drop_target_if_exists=True
        )
        is False
    )
    assert calls == ["exists"]
