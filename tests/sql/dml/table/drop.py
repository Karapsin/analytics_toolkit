from __future__ import annotations

import importlib
from copy import deepcopy
from typing import Any

import pytest
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import DropScopeState, install_drop_scope
from tests.sql._support.fakes import FakeDbapiConnection

drop_module = importlib.import_module("analytics_toolkit.sql.dml.table.drop_tables")
sql_module = importlib.import_module("analytics_toolkit.sql")

TARGET_TABLE = "analytics.events"
TARGET_TABLE_2 = "analytics.events_archive"
TARGET_SHARD_TABLE = "analytics.events_shard"


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch, write_sql_connections) -> DropScopeState:
    return install_drop_scope(monkeypatch, write_sql_connections)


def _assert_pending_drop(plan) -> None:
    # Guessed companions/local mirrors are superseded by execution-time ownership.
    assert plan.sqls == []
    assert plan.statements == []
    assert plan.metadata.statement_count is None
    assert plan.options["pending_existing_layout_verification"] is True
    assert plan.options["pending_resource_scope_resolution"] is True


def _drop_resources(commands: list[str]) -> list[tuple[str, str, str | None]]:
    resources = []
    for statement in commands:
        drop = parse_one(strip_query_label(statement), read=ClickHouseDialect)
        assert isinstance(drop, exp.Drop)
        cluster = drop.find(exp.OnCluster)
        tables = drop.args.get("tables") or [drop.this]
        resources.extend(
            (table.db, table.name, cluster.this.name if cluster else None) for table in tables
        )
    return resources


def _assert_closed_once(state: DropScopeState) -> None:
    assert len(state.opened) == 1
    assert state.opened[0].closed
    assert state.opened[0].close_calls == 1


def test_drop_tables_is_exported() -> None:
    assert sql_module.drop_tables is drop_module.drop_tables


@pytest.mark.parametrize(
    ("value", "error", "message"),
    [
        (("analytics.events",), TypeError, "string or a list"),
        ([], ValueError, "must not be empty"),
        (["   "], ValueError, "must not be empty"),
    ],
)
def test_drop_tables_rejects_invalid_table_collections(
    value: Any,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        drop_module._normalize_target_tables(value)


def test_drop_tables_dry_run_builds_default_pair_drop_sqls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        drop_module,
        "get_sql_connection",
        lambda key: pytest.fail("connection should not be opened"),
    )

    plan = drop_module.drop_tables(
        "ch",
        TARGET_TABLE,
        ch_cluster="analytics",
        dry_run=True,
    )

    assert plan.operation == "drop_tables"
    assert plan.target_alias == "ch"
    assert plan.target_backend == "ch"
    assert plan.target_table == TARGET_TABLE
    assert plan.options["if_exists"] is False
    assert plan.options["ch_cluster"] == "analytics"
    assert plan.options["ch_drop_shard"] is True
    assert plan.options["ch_drop_distributed"] is True
    _assert_pending_drop(plan)


def test_drop_tables_dry_run_can_drop_only_distributed_table() -> None:
    plan = drop_module.drop_tables(
        "ch",
        TARGET_TABLE,
        ch_cluster=None,
        ch_drop_shard=False,
        if_exists=True,
        return_sql=True,
    )

    assert plan.options["ch_cluster"] is None
    assert plan.options["ch_drop_shard"] is False
    assert plan.options["if_exists"] is True
    assert plan.options["ch_drop_distributed"] is True
    _assert_pending_drop(plan)


def test_drop_tables_dry_run_accepts_shard_table_as_target() -> None:
    plan = drop_module.drop_tables(
        "ch",
        TARGET_SHARD_TABLE,
        dry_run=True,
    )

    assert plan.target_table == TARGET_SHARD_TABLE
    assert plan.options["ch_cluster"] == "{cluster}"
    _assert_pending_drop(plan)


def test_drop_tables_dry_run_accepts_list_of_tables() -> None:
    plan = drop_module.drop_tables(
        "ch",
        [TARGET_TABLE, TARGET_TABLE_2],
        ch_cluster=None,
        if_exists=True,
        dry_run=True,
    )

    assert plan.options["tables"] == [TARGET_TABLE, TARGET_TABLE_2]
    assert plan.options["if_exists"] is True
    assert plan.options["ch_cluster"] is None
    _assert_pending_drop(plan)


def test_drop_tables_dry_run_applies_query_label() -> None:
    plan = drop_module.drop_tables(
        "ch",
        TARGET_TABLE,
        dry_run=True,
        query_label="drop-events",
    )

    assert plan.metadata.query_label == "drop-events"
    _assert_pending_drop(plan)


def test_drop_tables_executes_pair_drop_sqls(
    fake_client: DropScopeState,
) -> None:
    before = fake_client.snapshot()
    expected = deepcopy(before)
    expected[fake_client.coordinator].pop(TARGET_TABLE)
    expected[fake_client.remote].pop(TARGET_SHARD_TABLE)
    result = drop_module.drop_tables(
        "ch",
        TARGET_TABLE,
        ch_cluster="storage",
        if_exists=True,
    )

    assert result is None
    assert _drop_resources(fake_client.commands) == [
        ("analytics", "events", None),
        ("analytics", "events_shard", "storage"),
    ]
    assert fake_client.command_settings[0]["skip_unavailable_shards"] == 0
    settings = fake_client.command_settings[1]
    assert settings["skip_unavailable_shards"] == 0
    assert settings["distributed_ddl_task_timeout"] > 0
    assert settings["distributed_ddl_output_mode"] == "throw"
    assert fake_client.snapshot() == expected
    assert fake_client.rows(fake_client.coordinator, TARGET_SHARD_TABLE) == fake_client.foreign_rows
    assert "SELECT serverUUID(), tcpPort()" in fake_client.queries
    _assert_closed_once(fake_client)


def test_drop_tables_executes_shard_table_target_as_single_local_drop(
    fake_client: DropScopeState,
) -> None:
    before = fake_client.snapshot()
    expected = deepcopy(before)
    expected[fake_client.coordinator].pop(TARGET_SHARD_TABLE)
    drop_module.drop_tables(
        "ch",
        TARGET_SHARD_TABLE,
        if_exists=True,
    )

    assert _drop_resources(fake_client.commands) == [("analytics", "events_shard", None)]
    assert fake_client.command_settings == [{"skip_unavailable_shards": 0}]
    assert fake_client.snapshot() == expected
    assert fake_client.rows(fake_client.remote, TARGET_SHARD_TABLE) == [(11,), (12,), (13,)]
    assert fake_client.rows(fake_client.coordinator, TARGET_TABLE) == [(11,), (12,), (13,)]
    _assert_closed_once(fake_client)


def test_drop_tables_return_metadata_tracks_operation(
    fake_client: DropScopeState,
) -> None:
    before = fake_client.snapshot()
    expected = deepcopy(before)
    expected[fake_client.coordinator].pop(TARGET_TABLE)
    result = drop_module.drop_tables(
        "ch",
        TARGET_TABLE,
        ch_cluster=None,
        ch_drop_shard=False,
        if_exists=True,
        return_metadata=True,
        query_label="drop-meta",
    )

    assert result.rows is None
    assert result.plan.operation == "drop_tables"
    assert result.metadata.statement_count is None
    assert result.metadata.elapsed_seconds >= 0
    assert result.metadata.operation_status == "success"
    assert result.metadata.query_label == "drop-meta"
    assert _drop_resources(fake_client.commands) == [("analytics", "events", None)]
    assert all(
        statement.startswith("/* analytics_toolkit query_label=drop-meta */")
        for statement in fake_client.commands
    )
    assert fake_client.snapshot() == expected
    assert fake_client.rows(fake_client.coordinator, TARGET_SHARD_TABLE) == fake_client.foreign_rows
    assert fake_client.rows(fake_client.remote, TARGET_SHARD_TABLE) == [(11,), (12,), (13,)]
    _assert_closed_once(fake_client)


def test_drop_tables_dry_run_accepts_non_clickhouse_alias() -> None:
    plan = drop_module.drop_tables(
        "gp",
        "public.events",
        dry_run=True,
    )

    assert plan.operation == "drop_tables"
    assert plan.target_backend == "gp"
    assert plan.options["if_exists"] is False
    assert plan.sqls == ["DROP TABLE public.events"]


def test_drop_tables_executes_non_clickhouse_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        drop_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    result = drop_module.drop_tables(
        "gp",
        "public.events",
        if_exists=True,
    )

    assert result is None
    assert connection.executed == ["DROP TABLE IF EXISTS public.events"]
    assert connection.close_calls == 1
