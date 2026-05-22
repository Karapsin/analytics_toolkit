from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ch_drop_module = importlib.import_module(
    "analytics_toolkit.sql.dml.table.ch_drop_table"
)
sql_module = importlib.import_module("analytics_toolkit.sql")

from analytics_toolkit.sql.connection.errors import UnsupportedConnectionTypeError


TARGET_TABLE = "analytics.events"
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
        ch_drop_module,
        "get_sql_connection",
        lambda connection_key: client,
    )
    return client


def test_ch_drop_table_is_exported() -> None:
    assert sql_module.ch_drop_table is ch_drop_module.ch_drop_table


def test_ch_drop_table_dry_run_builds_default_pair_drop_sqls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ch_drop_module,
        "get_sql_connection",
        lambda key: pytest.fail("connection should not be opened"),
    )

    plan = ch_drop_module.ch_drop_table(
        "ch",
        TARGET_TABLE,
        ch_cluster="analytics",
        dry_run=True,
    )

    assert plan.operation == "ch_drop_table"
    assert plan.target_alias == "ch"
    assert plan.target_backend == "ch"
    assert plan.target_table == TARGET_TABLE
    assert plan.options["shard_table"] == TARGET_SHARD_TABLE
    assert plan.options["ch_cluster"] == "analytics"
    assert plan.metadata.statement_count == 4
    assert [statement.phase for statement in plan.statements] == [
        "drop_table",
        "drop_table",
        "drop_table",
        "drop_table",
    ]
    assert plan.sqls == [
        "DROP TABLE IF EXISTS analytics.events",
        "DROP TABLE IF EXISTS analytics.events_shard",
        "DROP TABLE IF EXISTS analytics.events ON CLUSTER analytics",
        "DROP TABLE IF EXISTS analytics.events_shard ON CLUSTER analytics",
    ]


def test_ch_drop_table_dry_run_can_skip_cluster_and_override_shard() -> None:
    plan = ch_drop_module.ch_drop_table(
        "ch",
        TARGET_TABLE,
        ch_cluster=None,
        shard_table="analytics.events_local",
        return_sql=True,
    )

    assert plan.options["shard_table"] == "analytics.events_local"
    assert plan.options["ch_cluster"] is None
    assert plan.metadata.statement_count == 2
    assert plan.sqls == [
        "DROP TABLE IF EXISTS analytics.events",
        "DROP TABLE IF EXISTS analytics.events_local",
    ]


def test_ch_drop_table_dry_run_applies_query_label() -> None:
    plan = ch_drop_module.ch_drop_table(
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


def test_ch_drop_table_executes_pair_drop_sqls(
    fake_client: FakeClickHouseClient,
) -> None:
    ch_drop_module.ch_drop_table(
        "ch",
        TARGET_TABLE,
        ch_cluster="{cluster}",
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


def test_ch_drop_table_return_metadata_tracks_operation(
    fake_client: FakeClickHouseClient,
) -> None:
    result = ch_drop_module.ch_drop_table(
        "ch",
        TARGET_TABLE,
        ch_cluster=None,
        return_metadata=True,
        query_label="drop-meta",
    )

    assert result.rows is None
    assert result.plan.operation == "ch_drop_table"
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


def test_ch_drop_table_rejects_non_clickhouse_alias() -> None:
    with pytest.raises(UnsupportedConnectionTypeError, match="requires a ch connection"):
        ch_drop_module.ch_drop_table("gp", TARGET_TABLE)
