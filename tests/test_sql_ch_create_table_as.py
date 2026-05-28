from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ch_ctas_module = importlib.import_module(
    "analytics_toolkit.sql.dml.table.ch_create_table_as"
)
sql_module = importlib.import_module("analytics_toolkit.sql")


TARGET_TABLE = "default.events_result"
TARGET_SHARD_TABLE = "default.events_result_shard"
QUERY = """
SELECT
    dt,
    id,
    amount
FROM default.events_source
WHERE amount > 0
""".strip()
CTE_JOIN_QUERY = """
WITH trigger_map AS (
    SELECT 1 AS id
)
SELECT
    events.id,
    trigger_map.kind
FROM default.events_source AS events
LEFT JOIN trigger_map AS trigger_map
    ON events.id = trigger_map.id
""".strip()


class FakeClickHouseResult:
    def __init__(
        self,
        result_rows: list[tuple[Any, ...]],
        column_names: tuple[str, ...] = (),
        column_types: tuple[Any, ...] = (),
    ) -> None:
        self.result_rows = result_rows
        self.column_names = column_names
        self.column_types = column_types


class FakeDatabaseError(Exception):
    pass


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.command_settings: list[dict[str, object] | None] = []
        self.queries: list[str] = []
        self.created_tables: set[str] = set()
        self.close_calls = 0
        self.insert_error: Exception | None = None
        self.schema_query_error: Exception | None = None

    def command(
        self,
        sql: str,
        settings: dict[str, object] | None = None,
    ) -> None:
        self.commands.append(sql)
        self.command_settings.append(settings)
        self._track_table_ddl(sql)
        if self.insert_error is not None and sql.startswith("INSERT INTO"):
            raise self.insert_error

    def query(self, sql: str) -> FakeClickHouseResult:
        self.queries.append(sql)
        if sql.startswith("SELECT getMacro("):
            return FakeClickHouseResult([("core",)])
        if sql.startswith("SELECT *\nFROM (\n"):
            if self.schema_query_error is not None:
                raise self.schema_query_error
            return FakeClickHouseResult(
                [],
                column_names=("dt", "id", "amount"),
                column_types=(
                    type("FakeType", (), {"name": "Date"})(),
                    type("FakeType", (), {"name": "UInt64"})(),
                    type("FakeType", (), {"name": "Decimal(18, 4)"})(),
                ),
            )
        if "clusterAllReplicas" in sql and "system, one" in sql:
            return FakeClickHouseResult([(1,)])
        if "FROM system.clusters" in sql:
            return FakeClickHouseResult([(1,)])
        if "clusterAllReplicas" in sql and "system, tables" in sql:
            return FakeClickHouseResult([(self._cluster_table_count(sql),)])
        if "clusterAllReplicas" in sql:
            return FakeClickHouseResult([(1,)])
        if sql.startswith("EXISTS TABLE "):
            table_name = sql[len("EXISTS TABLE "):].strip()
            return FakeClickHouseResult([(int(table_name in self.created_tables),)])
        raise AssertionError(f"Unexpected query: {sql}")

    def close(self) -> None:
        self.close_calls += 1

    def _track_table_ddl(self, sql: str) -> None:
        body = _strip_query_label(sql)
        if body.startswith("CREATE TABLE IF NOT EXISTS "):
            table_name = body[len("CREATE TABLE IF NOT EXISTS "):].split()[0]
            self.created_tables.add(table_name)
            return
        if body.startswith("DROP TABLE IF EXISTS "):
            table_name = body[len("DROP TABLE IF EXISTS "):].split()[0]
            self.created_tables.discard(table_name)

    def _cluster_table_count(self, sql: str) -> int:
        marker = "AND name = '"
        if marker not in sql:
            return len(self.created_tables)
        relation_name = sql.split(marker, 1)[1].split("'", 1)[0]
        return sum(
            1
            for table_name in self.created_tables
            if table_name.rsplit(".", 1)[-1] == relation_name
        )


def _strip_query_label(sql: str) -> str:
    stripped = sql.lstrip()
    if stripped.startswith("/* analytics_toolkit query_label=") and "*/" in stripped:
        return stripped.split("*/", 1)[1].lstrip()
    return stripped


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeClickHouseClient:
    client = FakeClickHouseClient()
    monkeypatch.setattr(
        ch_ctas_module,
        "get_sql_connection",
        lambda connection_key: client,
    )
    return client


def test_ch_create_table_as_is_exported() -> None:
    assert sql_module.ch_create_table_as is ch_ctas_module.ch_create_table_as


def test_ch_create_table_as_creates_pair_and_inserts_query(
    fake_client: FakeClickHouseClient,
) -> None:
    ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY + ";",
        partition_by=["dt"],
        order_by=["dt", "id"],
        sharding_key="cityHash64(dt, id)",
    )

    assert fake_client.commands[:4] == [
        f"DROP TABLE IF EXISTS {TARGET_TABLE}",
        f"DROP TABLE IF EXISTS {TARGET_SHARD_TABLE}",
        f"DROP TABLE IF EXISTS {TARGET_TABLE} ON CLUSTER '{{cluster}}'",
        f"DROP TABLE IF EXISTS {TARGET_SHARD_TABLE} ON CLUSTER '{{cluster}}'",
    ]
    assert fake_client.command_settings[2] == {
        "distributed_ddl_task_timeout": 0,
        "distributed_ddl_output_mode": "none",
    }
    shard_sql, local_shard_sql, distributed_sql, local_distributed_sql = (
        fake_client.commands[4:8]
    )
    assert shard_sql.startswith(f"CREATE OR REPLACE TABLE {TARGET_SHARD_TABLE}")
    assert "ON CLUSTER '{cluster}'" in shard_sql
    assert "ENGINE = ReplicatedMergeTree" in shard_sql
    assert "PARTITION BY `dt`" in shard_sql
    assert "ORDER BY (`dt`, `id`)" in shard_sql
    assert "`dt` Date" in shard_sql
    assert "`id` UInt64" in shard_sql
    assert "`amount` Decimal(18, 4)" in shard_sql
    assert QUERY not in shard_sql
    assert local_shard_sql.startswith(
        f"CREATE TABLE IF NOT EXISTS {TARGET_SHARD_TABLE}"
    )
    assert "ON CLUSTER" not in local_shard_sql
    assert "UUID '" in local_shard_sql
    assert "ENGINE = ReplicatedMergeTree" in local_shard_sql
    assert distributed_sql.startswith(f"CREATE OR REPLACE TABLE {TARGET_TABLE}")
    assert "ON CLUSTER '{cluster}'" in distributed_sql
    assert "ENGINE = Distributed(" in distributed_sql
    assert "    '{cluster}'," in distributed_sql
    assert "    'default'," in distributed_sql
    assert "    'events_result_shard'," in distributed_sql
    assert "    cityHash64(dt, id)" in distributed_sql
    assert "`amount` Decimal(18, 4)" in distributed_sql
    assert QUERY not in distributed_sql
    assert local_distributed_sql.startswith(
        f"CREATE TABLE IF NOT EXISTS {TARGET_TABLE}"
    )
    assert "ON CLUSTER" not in local_distributed_sql
    assert fake_client.commands[8] == f"INSERT INTO {TARGET_TABLE}\n{QUERY}"
    assert fake_client.command_settings[4] == {
        "distributed_ddl_task_timeout": 0,
        "distributed_ddl_output_mode": "none",
    }
    assert (
        "SELECT *\n"
        "FROM (\n"
        f"{QUERY}\n"
        ") AS _ch_create_table_as_source\n"
        "LIMIT 0"
    ) in fake_client.queries
    assert f"EXISTS TABLE {TARGET_TABLE}" in fake_client.queries
    assert f"EXISTS TABLE {TARGET_SHARD_TABLE}" in fake_client.queries
    assert any("clusterAllReplicas" in query for query in fake_client.queries)
    assert fake_client.close_calls == 1


def test_ch_create_table_as_table_schema_overrides_inferred_types(
    fake_client: FakeClickHouseClient,
) -> None:
    ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY,
        table_schema={"dt": "String", "id": "String", "amount": "Float64"},
    )

    shard_sql = fake_client.commands[4]
    assert "`dt` String" in shard_sql
    assert "`id` String" in shard_sql
    assert "`amount` Float64" in shard_sql
    assert "`dt` Date" not in shard_sql
    assert any(
        query.startswith("SELECT *\nFROM (\n") for query in fake_client.queries
    )


def test_ch_create_table_as_dry_run_uses_table_schema() -> None:
    plan = ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY,
        dry_run=True,
        table_schema={"dt": "Date", "id": "UInt64", "amount": "Decimal(18, 4)"},
    )

    create_sql = next(
        statement.sql for statement in plan.statements if statement.phase == "create_target"
    )
    assert plan.options["table_schema"] == {
        "dt": "Date",
        "id": "UInt64",
        "amount": "Decimal(18, 4)",
    }
    assert "`dt` Date" in create_sql
    assert "`id` UInt64" in create_sql
    assert "`amount` Decimal(18, 4)" in create_sql


def test_ch_create_table_as_quotes_cluster_macro(
    fake_client: FakeClickHouseClient,
) -> None:
    ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY,
    )

    assert f"DROP TABLE IF EXISTS {TARGET_TABLE} ON CLUSTER '{{cluster}}'" in (
        fake_client.commands
    )
    shard_sql, _, distributed_sql, _ = fake_client.commands[4:8]
    assert "ON CLUSTER '{cluster}'" in shard_sql
    assert "ON CLUSTER '{cluster}'" in distributed_sql
    assert "    '{cluster}'," in distributed_sql


def test_ch_create_table_as_rejects_multiple_statements(
    fake_client: FakeClickHouseClient,
) -> None:
    with pytest.raises(
        ch_ctas_module.InvalidSqlInputError,
        match="exactly one SQL statement",
    ):
        ch_ctas_module.ch_create_table_as(
            "ch",
            TARGET_TABLE,
            "SELECT 1; SELECT 2",
        )

    assert fake_client.commands == []
    assert fake_client.close_calls == 0


def test_ch_create_table_as_rejects_non_clickhouse_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ch_ctas_module,
        "get_sql_connection",
        lambda connection_key: pytest.fail("connection should not be opened"),
    )

    with pytest.raises(
        ch_ctas_module.UnsupportedConnectionTypeError,
        match="requires a ch",
    ):
        ch_ctas_module.ch_create_table_as("gp", TARGET_TABLE, QUERY)


def test_ch_create_table_as_final_insert_cte_unknown_table_keeps_exception_type(
    fake_client: FakeClickHouseClient,
) -> None:
    error = FakeDatabaseError(
        "Code: 60. DB::Exception: Table default.trigger_map does not exist. "
        "(UNKNOWN_TABLE)"
    )
    fake_client.insert_error = error

    with pytest.raises(FakeDatabaseError) as exc_info:
        ch_ctas_module.ch_create_table_as("ch", TARGET_TABLE, CTE_JOIN_QUERY)

    assert exc_info.value is error
    notes = "\n".join(getattr(exc_info.value, "__notes__", ()))
    assert "ClickHouse could not resolve CTE 'trigger_map'" in notes
    assert "GLOBAL LEFT JOIN trigger_map AS alias" in notes


def test_ch_create_table_as_final_insert_unknown_table_without_cte_has_no_hint(
    fake_client: FakeClickHouseClient,
) -> None:
    error = FakeDatabaseError(
        "Code: 60. DB::Exception: Table default.missing_map does not exist. "
        "(UNKNOWN_TABLE)"
    )
    fake_client.insert_error = error

    with pytest.raises(FakeDatabaseError) as exc_info:
        ch_ctas_module.ch_create_table_as("ch", TARGET_TABLE, CTE_JOIN_QUERY)

    assert exc_info.value is error
    notes = "\n".join(getattr(exc_info.value, "__notes__", ()))
    assert "GLOBAL LEFT JOIN" not in notes


def test_ch_create_table_as_schema_inference_cte_unknown_table_adds_hint(
    fake_client: FakeClickHouseClient,
) -> None:
    error = FakeDatabaseError(
        "Code: 60. DB::Exception: Table default.trigger_map does not exist. "
        "(UNKNOWN_TABLE)"
    )
    fake_client.schema_query_error = error

    with pytest.raises(FakeDatabaseError) as exc_info:
        ch_ctas_module.ch_create_table_as("ch", TARGET_TABLE, CTE_JOIN_QUERY)

    assert exc_info.value is error
    notes = "\n".join(getattr(exc_info.value, "__notes__", ()))
    assert "ClickHouse could not resolve CTE 'trigger_map'" in notes
    assert "GLOBAL LEFT JOIN trigger_map AS alias" in notes
