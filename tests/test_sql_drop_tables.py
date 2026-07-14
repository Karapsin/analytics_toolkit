from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

drop_module = importlib.import_module(
    "analytics_toolkit.sql.dml.table.drop_tables"
)
sql_module = importlib.import_module("analytics_toolkit.sql")

from tests.sql_fakes import FakeDbapiConnection


TARGET_TABLE = "analytics.events"
TARGET_TABLE_2 = "analytics.events_archive"
TARGET_SHARD_TABLE = "analytics.events_shard"


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.command_settings: list[dict[str, object] | None] = []
        self.queries: list[str] = []
        self.close_calls = 0

    def command(
        self,
        sql: str,
        settings: dict[str, object] | None = None,
    ) -> None:
        self.commands.append(sql)
        self.command_settings.append(settings)

    def query(self, sql: str) -> Any:
        self.queries.append(sql)
        return []

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeClickHouseClient:
    client = FakeClickHouseClient()
    monkeypatch.setattr(
        drop_module,
        "get_sql_connection",
        lambda connection_key: client,
    )
    return client


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
    assert plan.metadata.statement_count == 4
    assert [statement.phase for statement in plan.statements] == [
        "drop_tables",
        "drop_tables",
        "drop_tables",
        "drop_tables",
    ]
    assert plan.sqls == [
        "DROP TABLE analytics.events",
        "DROP TABLE analytics.events_shard",
        "DROP TABLE analytics.events ON CLUSTER analytics",
        "DROP TABLE analytics.events_shard ON CLUSTER analytics",
    ]


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
    assert plan.metadata.statement_count == 1
    assert plan.sqls == [
        "DROP TABLE IF EXISTS analytics.events",
    ]


def test_drop_tables_dry_run_accepts_shard_table_as_target() -> None:
    plan = drop_module.drop_tables(
        "ch",
        TARGET_SHARD_TABLE,
        dry_run=True,
    )

    assert plan.metadata.statement_count == 1
    assert plan.sqls == ["DROP TABLE analytics.events_shard"]


def test_drop_tables_dry_run_accepts_list_of_tables() -> None:
    plan = drop_module.drop_tables(
        "ch",
        [TARGET_TABLE, TARGET_TABLE_2],
        ch_cluster=None,
        if_exists=True,
        dry_run=True,
    )

    assert plan.options["tables"] == [TARGET_TABLE, TARGET_TABLE_2]
    assert plan.metadata.statement_count == 4
    assert plan.sqls == [
        "DROP TABLE IF EXISTS analytics.events",
        "DROP TABLE IF EXISTS analytics.events_shard",
        "DROP TABLE IF EXISTS analytics.events_archive",
        "DROP TABLE IF EXISTS analytics.events_archive_shard",
    ]


def test_drop_tables_dry_run_applies_query_label() -> None:
    plan = drop_module.drop_tables(
        "ch",
        TARGET_TABLE,
        dry_run=True,
        query_label="drop-events",
    )

    assert plan.metadata.query_label == "drop-events"
    assert len(plan.sqls) == 4
    assert all(
        sql.startswith("/* analytics_toolkit query_label=drop-events */")
        for sql in plan.sqls
    )


def test_drop_tables_executes_pair_drop_sqls(
    fake_client: FakeClickHouseClient,
) -> None:
    drop_module.drop_tables(
        "ch",
        TARGET_TABLE,
        ch_cluster="{cluster}",
        if_exists=True,
    )

    assert fake_client.commands == [
        "DROP TABLE IF EXISTS analytics.events",
        "DROP TABLE IF EXISTS analytics.events_shard",
        "DROP TABLE IF EXISTS analytics.events ON CLUSTER '{cluster}'",
        "DROP TABLE IF EXISTS analytics.events_shard ON CLUSTER '{cluster}'",
    ]
    assert fake_client.command_settings[2] == {
        "distributed_ddl_task_timeout": 0,
        "distributed_ddl_output_mode": "none",
    }
    assert fake_client.command_settings[3] == {
        "distributed_ddl_task_timeout": 0,
        "distributed_ddl_output_mode": "none",
    }
    assert fake_client.queries == []
    assert fake_client.close_calls == 1


def test_drop_tables_executes_shard_table_target_as_single_local_drop(
    fake_client: FakeClickHouseClient,
) -> None:
    drop_module.drop_tables(
        "ch",
        TARGET_SHARD_TABLE,
        if_exists=True,
    )

    assert fake_client.commands == [
        "DROP TABLE IF EXISTS analytics.events_shard",
    ]
    assert fake_client.command_settings == [None]
    assert fake_client.queries == []
    assert fake_client.close_calls == 1


def test_drop_tables_return_metadata_tracks_operation(
    fake_client: FakeClickHouseClient,
) -> None:
    result = drop_module.drop_tables(
        "ch",
        TARGET_TABLE,
        ch_cluster=None,
        if_exists=True,
        return_metadata=True,
        query_label="drop-meta",
    )

    assert result.rows is None
    assert result.plan.operation == "drop_tables"
    assert result.metadata.statement_count == 2
    assert result.metadata.elapsed_seconds >= 0
    assert result.metadata.operation_status == "success"
    assert result.metadata.query_label == "drop-meta"
    assert fake_client.commands == [
        "/* analytics_toolkit query_label=drop-meta */\n"
        "DROP TABLE IF EXISTS analytics.events",
        "/* analytics_toolkit query_label=drop-meta */\n"
        "DROP TABLE IF EXISTS analytics.events_shard",
    ]


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
