from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

create_module = importlib.import_module("analytics_toolkit.sql.dml.table.create_table_from_sql")
ch_fast_path_module = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.create_table_from_sql"
)
sql_module = importlib.import_module("analytics_toolkit.sql")


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class FakeDbapiCursor:
    def __init__(self, connection: FakeDbapiConnection) -> None:
        self.connection = connection
        self.description = connection.description
        self.rowcount = -1
        self.close_calls = 0

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.connection.executed.append(sql)
        self.connection.executed_params.append(params)
        if sql.startswith("INSERT INTO "):
            self.rowcount = self.connection.insert_rowcount

    def fetchone(self) -> tuple[Any, ...] | None:
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []

    def close(self) -> None:
        self.close_calls += 1


class FakeDbapiConnection:
    def __init__(
        self,
        description: list[tuple[Any, ...]] | None = None,
        insert_rowcount: int = 0,
    ) -> None:
        self.description = description or []
        self.insert_rowcount = insert_rowcount
        self.executed: list[str] = []
        self.executed_params: list[tuple[Any, ...] | None] = []
        self.cursors: list[FakeDbapiCursor] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self) -> FakeDbapiCursor:
        cursor = FakeDbapiCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class CloseFailDbapiConnection(FakeDbapiConnection):
    def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("connection close failed")


class FakeClickHouseClient:
    def __init__(self, insert_rowcount: int = 0) -> None:
        self.insert_rowcount = insert_rowcount
        self.commands: list[str] = []
        self.command_settings: list[dict[str, object] | None] = []
        self.queries: list[str] = []
        self.created_tables: set[str] = set()
        self.close_calls = 0

    def command(
        self,
        sql: str,
        settings: dict[str, object] | None = None,
    ) -> dict[str, int] | None:
        self.commands.append(sql)
        self.command_settings.append(settings)
        self._track_table_ddl(sql)
        if sql.startswith("INSERT INTO "):
            return {"written_rows": self.insert_rowcount}
        return None

    def query(self, sql: str) -> FakeResult:
        self.queries.append(sql)
        if sql.startswith("SELECT getMacro("):
            return FakeResult([("core",)])
        if "clusterAllReplicas" in sql and "system, one" in sql:
            return FakeResult([(1,)])
        if "FROM system.clusters" in sql:
            return FakeResult([(1,)])
        if "clusterAllReplicas" in sql and "system, tables" in sql:
            return FakeResult([(self._cluster_table_count(sql),)])
        if "clusterAllReplicas" in sql and "system, columns" in sql:
            return FakeResult([(sql.count("name = ") or 1,)])
        if "clusterAllReplicas" in sql:
            return FakeResult([(len(self.created_tables),)])
        if sql.startswith("EXISTS TABLE "):
            table_name = sql[len("EXISTS TABLE ") :].strip()
            return FakeResult([(int(table_name in self.created_tables),)])
        return FakeResult([])

    def close(self) -> None:
        self.close_calls += 1

    def _track_table_ddl(self, sql: str) -> None:
        body = _strip_query_label(sql)
        if body.startswith("CREATE TABLE IF NOT EXISTS "):
            table_name = body[len("CREATE TABLE IF NOT EXISTS ") :].split()[0]
            self.created_tables.add(table_name)
            return
        if body.startswith("DROP TABLE IF EXISTS "):
            table_name = body[len("DROP TABLE IF EXISTS ") :].split()[0]
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


SOURCE_DESCRIPTION = [
    ("id", 23, None, None, None, None),
    ("amount", 1700, None, None, 12, 2),
]


def test_create_table_from_sql_is_not_public() -> None:
    assert "create_table_from_sql" not in sql_module.__all__
    assert not hasattr(sql_module, "create_table_from_sql")


def test_schema_only_creation_uses_native_metadata_types(monkeypatch) -> None:
    connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )
    result = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table;",
        insert_data=False,
    )

    assert result is None
    assert connection.executed[0] == (
        "SELECT * FROM (select id, amount from source_table) AS source_schema_probe WHERE 1 = 0"
    )
    assert any(
        sql.startswith("CREATE TABLE sandbox.target_table")
        and '"id" INTEGER' in sql
        and '"amount" NUMERIC(12, 2)' in sql
        for sql in connection.executed
    )
    assert not any(sql.startswith("DROP TABLE") for sql in connection.executed)
    assert connection.close_calls == 1


def test_create_table_from_sql_table_schema_overrides_source_types(
    monkeypatch,
) -> None:
    connection = FakeDbapiConnection(
        description=SOURCE_DESCRIPTION,
        insert_rowcount=3,
    )
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    inserted_rows = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table",
        insert_data=True,
        table_schema={"id": "TEXT", "amount": "DOUBLE PRECISION"},
    )

    assert inserted_rows == 3
    assert any(
        sql.startswith("CREATE TABLE sandbox.target_table")
        and '"id" TEXT' in sql
        and '"amount" DOUBLE PRECISION' in sql
        for sql in connection.executed
    )
    assert connection.executed[-1] == (
        'INSERT INTO sandbox.target_table ("id", "amount") '
        'SELECT CAST("id" AS TEXT) AS "id", '
        'CAST("amount" AS DOUBLE PRECISION) AS "amount" '
        "FROM (select id, amount from source_table) AS source_query"
    )


def test_create_table_from_sql_dry_run_uses_table_schema() -> None:
    plan = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table",
        insert_data=False,
        dry_run=True,
        table_schema={"id": "TEXT", "amount": "NUMERIC(10, 2)"},
    )

    create_sql = next(
        statement.sql for statement in plan.statements if statement.phase == "create_target"
    )
    assert plan.options["table_schema"] == {
        "id": "TEXT",
        "amount": "NUMERIC(10, 2)",
    }
    assert '"id" TEXT' in create_sql
    assert '"amount" NUMERIC(10, 2)' in create_sql


def test_schema_only_creation_falls_back_for_gp_unbounded_numeric(
    monkeypatch,
) -> None:
    connection = FakeDbapiConnection(
        description=[
            ("quantity", 1700, None, None, 65535, 0),
        ]
    )
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select quantity from source_table;",
        insert_data=False,
    )

    assert any(
        sql.startswith("CREATE TABLE sandbox.target_table")
        and '"quantity" NUMERIC' in sql
        and '"quantity" NUMERIC(' not in sql
        for sql in connection.executed
    )


def test_schema_only_creation_preserves_gp_bytea_columns(monkeypatch) -> None:
    connection = FakeDbapiConnection(
        description=[
            ("cheque_pk", 17, None, None, None, None),
            ("quantity", 1700, None, None, 12, 3),
        ]
    )
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select cheque_pk, quantity from source_table;",
        insert_data=False,
    )

    assert any(
        sql.startswith("CREATE TABLE sandbox.target_table")
        and '"cheque_pk" BYTEA' in sql
        and '"quantity" NUMERIC(12, 3)' in sql
        for sql in connection.executed
    )


def test_cross_backend_creation_maps_types_to_clickhouse_and_creates_pair(
    monkeypatch,
) -> None:
    source = FakeDbapiConnection(
        description=[
            ("id", 23, None, None, None, None),
            ("dt", 1082, None, None, None, None),
            ("amount", 1700, None, None, 12, 2),
        ]
    )
    target = FakeClickHouseClient()

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)

    create_module.create_table_from_sql(
        "gp",
        "analytics.events",
        "select id, dt, amount from source_table",
        table_db="ch",
        insert_data=False,
        partition_by=["dt"],
        order_by=["dt", "id"],
        ch_sharding_key="cityHash64(id)",
    )

    assert not any(command.startswith("DROP TABLE") for command in target.commands)
    assert len(target.commands) == 4
    shard_sql, local_shard_sql, distributed_sql, local_distributed_sql = target.commands
    assert shard_sql.startswith("CREATE TABLE IF NOT EXISTS analytics.events_shard")
    assert "ON CLUSTER '{cluster}'" in shard_sql
    assert "`id` Nullable(Int32)" in shard_sql
    assert "`dt` Nullable(Date)" in shard_sql
    assert "`amount` Nullable(Decimal(12, 2))" in shard_sql
    assert "PARTITION BY `dt`" in shard_sql
    assert "ORDER BY (`dt`, `id`)" in shard_sql
    assert local_shard_sql.startswith("CREATE TABLE IF NOT EXISTS analytics.events_shard")
    assert "ON CLUSTER" not in local_shard_sql
    assert distributed_sql.startswith("CREATE TABLE IF NOT EXISTS analytics.events")
    assert "ENGINE = Distributed(" in distributed_sql
    assert "    'events_shard'," in distributed_sql
    assert "    cityHash64(id)" in distributed_sql
    assert "ON CLUSTER" not in local_distributed_sql
    assert "EXISTS TABLE analytics.events" in target.queries
    assert "EXISTS TABLE analytics.events_shard" in target.queries
    assert any("clusterAllReplicas" in query for query in target.queries)
    assert source.close_calls == 1
    assert target.close_calls == 1


def test_drop_target_if_exists_drops_trino_target_before_create(monkeypatch) -> None:
    source = FakeDbapiConnection(
        description=[
            ("id", 23, None, None, None, None),
            ("name", 25, None, None, None, None),
        ]
    )
    target = FakeDbapiConnection()

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "trino":
            return target
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)

    create_module.create_table_from_sql(
        "gp",
        "sandbox.created_table",
        "select id, name from source_table",
        table_db="trino",
        insert_data=False,
        drop_target_if_exists=True,
    )

    assert target.executed[0] == "DROP TABLE IF EXISTS sandbox.created_table"
    assert any(
        sql.startswith("CREATE TABLE sandbox.created_table")
        and '"id" INTEGER' in sql
        and '"name" VARCHAR' in sql
        for sql in target.executed
    )


def test_drop_target_if_exists_drops_clickhouse_distributed_pair(monkeypatch) -> None:
    source = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    target = FakeClickHouseClient()

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)

    create_module.create_table_from_sql(
        "gp",
        "analytics.events",
        "select id, amount from source_table",
        table_db="ch",
        insert_data=False,
        drop_target_if_exists=True,
    )

    assert target.commands[:4] == [
        "DROP TABLE IF EXISTS analytics.events",
        "DROP TABLE IF EXISTS analytics.events_shard",
        "DROP TABLE IF EXISTS analytics.events ON CLUSTER '{cluster}'",
        "DROP TABLE IF EXISTS analytics.events_shard ON CLUSTER '{cluster}'",
    ]


def test_insert_data_same_backend_emits_typed_insert_and_returns_rowcount(
    monkeypatch,
) -> None:
    connection = FakeDbapiConnection(
        description=SOURCE_DESCRIPTION,
        insert_rowcount=7,
    )
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    inserted_rows = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table",
        insert_data=True,
    )

    assert inserted_rows == 7
    assert connection.executed[-1] == (
        'INSERT INTO sandbox.target_table ("id", "amount") '
        'SELECT CAST("id" AS INTEGER) AS "id", '
        'CAST("amount" AS NUMERIC(12, 2)) AS "amount" '
        "FROM (select id, amount from source_table) AS source_query"
    )


def test_create_table_from_sql_inserts_by_default(monkeypatch) -> None:
    connection = FakeDbapiConnection(
        description=SOURCE_DESCRIPTION,
        insert_rowcount=4,
    )
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    inserted_rows = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table",
    )

    assert inserted_rows == 4
    assert connection.executed[-1].startswith("INSERT INTO sandbox.target_table")


def test_create_table_from_sql_same_clickhouse_delegates_to_adapter_fast_path(
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []
    probe_connection = FakeClickHouseClient()

    def fake_fast_path(**kwargs: object) -> tuple[bool, object]:
        calls.append(kwargs)
        return True, "delegated"

    monkeypatch.setattr(
        create_module.get_backend_adapter("ch"),
        "create_table_from_sql_fast_path",
        fake_fast_path,
    )
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: probe_connection,
    )

    result = create_module.create_table_from_sql(
        "ch",
        "analytics.events",
        "select id, amount from source_table;",
        order_by=["id"],
        insert_data=False,
        drop_target_if_exists=False,
        table_schema={"id": "UInt64", "amount": "Float64"},
    )

    assert result == "delegated"
    assert probe_connection.close_calls == 1
    assert calls == [
        {
            "source_backend": "ch",
            "source_key": "ch",
            "target_key": "ch",
            "target_table": "analytics.events",
            "source_sql": "select id, amount from source_table",
            "partition_by": None,
            "order_by": ["id"],
            "ch_engine": "ReplicatedMergeTree",
            "ch_cluster": "{cluster}",
            "ch_sharding_key": "rand()",
            "ch_only_shard": False,
            "ch_retry_per_host_drops": True,
            "insert_data": False,
            "drop_target_if_exists": False,
            "dry_run": False,
            "return_sql": False,
            "query_label": None,
            "return_metadata": False,
            "table_schema": {"id": "UInt64", "amount": "Float64"},
        }
    ]


def test_insert_data_cross_backend_delegates_to_transfer_after_creation(
    monkeypatch,
) -> None:
    source = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    target = FakeClickHouseClient()
    transfer_calls: list[dict[str, object]] = []

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    def fake_transfer_table(**kwargs: object) -> int:
        transfer_calls.append(kwargs)
        assert source.close_calls == 1
        assert target.close_calls == 1
        return 11

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(create_module, "transfer_table", fake_transfer_table)

    inserted_rows = create_module.create_table_from_sql(
        "gp",
        "analytics.events",
        "select id, amount from source_table;",
        table_db="ch",
        insert_data=True,
        order_by=["id"],
        trino_insert_chunk_size=500,
    )

    assert inserted_rows == 11
    assert transfer_calls == [
        {
            "from_db": "gp",
            "to_db": "ch",
            "from_sql": "select id, amount from source_table",
            "to_table": "analytics.events",
            "write_mode": "append",
            "gp_distributed_by_key": None,
            "trino_insert_chunk_size": 500,
            "partition_by": None,
            "order_by": ["id"],
            "ch_engine": "ReplicatedMergeTree",
            "ch_cluster": "{cluster}",
            "ch_sharding_key": "rand()",
            "retry_cnt": 1,
            "timeout_increment": 0,
            "full_retry_cnt": 1,
            "full_timeout_increment": 0,
        }
    ]
    assert any(
        command.startswith("CREATE TABLE IF NOT EXISTS analytics.events_shard")
        for command in target.commands
    )


def test_cross_backend_clickhouse_insert_false_creates_pair_without_transfer(
    monkeypatch,
) -> None:
    source = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    target = FakeClickHouseClient()

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(
        create_module,
        "transfer_table",
        lambda **kwargs: pytest.fail("transfer should not be called"),
    )

    result = create_module.create_table_from_sql(
        "gp",
        "analytics.events",
        "select id, amount from source_table",
        table_db="ch",
        insert_data=False,
        order_by=["id"],
    )

    assert result is None
    assert len(target.commands) == 4
    assert any(
        command.startswith("CREATE TABLE IF NOT EXISTS analytics.events_shard")
        for command in target.commands
    )
    assert not any(command.startswith("INSERT INTO ") for command in target.commands)


def test_create_table_from_sql_passes_table_schema_to_cross_backend_transfer(
    monkeypatch,
) -> None:
    source = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    target = FakeClickHouseClient()
    transfer_calls: list[dict[str, object]] = []

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    def fake_transfer_table(**kwargs: object) -> int:
        transfer_calls.append(kwargs)
        return 5

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(create_module, "transfer_table", fake_transfer_table)

    inserted_rows = create_module.create_table_from_sql(
        "gp",
        "analytics.events",
        "select id, amount from source_table",
        table_db="ch",
        insert_data=True,
        table_schema={"id": "String", "amount": "Float64"},
    )

    assert inserted_rows == 5
    assert transfer_calls[0]["table_schema"] == {
        "id": "String",
        "amount": "Float64",
    }
    assert any(
        command.startswith("CREATE TABLE IF NOT EXISTS analytics.events_shard")
        and "`id` String" in command
        and "`amount` Float64" in command
        for command in target.commands
    )


def test_create_table_from_sql_passes_only_shard_to_cross_backend_transfer(
    monkeypatch,
) -> None:
    source = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    target = FakeClickHouseClient()
    transfer_calls: list[dict[str, object]] = []

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    def fake_transfer_table(**kwargs: object) -> int:
        transfer_calls.append(kwargs)
        return 5

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(create_module, "transfer_table", fake_transfer_table)

    inserted_rows = create_module.create_table_from_sql(
        "gp",
        "analytics.events",
        "select id, amount from source_table",
        table_db="ch",
        insert_data=True,
        ch_only_shard=True,
        order_by=["id"],
    )

    assert inserted_rows == 5
    assert transfer_calls[0]["ch_only_shard"] is True
    assert any(
        command.startswith("CREATE TABLE IF NOT EXISTS analytics.events")
        and "ENGINE = ReplicatedMergeTree" in command
        for command in target.commands
    )
    assert not any("events_shard" in command for command in target.commands)
    assert not any("ENGINE = Distributed(" in command for command in target.commands)


def test_create_table_from_sql_validates_empty_inputs(monkeypatch) -> None:
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: pytest.fail("connection should not be opened"),
    )

    with pytest.raises(create_module.InvalidSqlInputError, match="table_name"):
        create_module.create_table_from_sql("gp", " ", "select 1")

    with pytest.raises(create_module.InvalidSqlInputError, match="sql"):
        create_module.create_table_from_sql("gp", "target", " ")

    with pytest.raises(create_module.InvalidSqlInputError, match="exactly one"):
        create_module.create_table_from_sql("gp", "target", "select 1; select 2")


def test_create_table_from_sql_retries_schema_inspection_with_fresh_connections(
    monkeypatch,
) -> None:
    connections: list[FakeDbapiConnection] = []
    inspection_calls = 0
    inspect_source_query_schema = create_module.inspect_source_query_schema

    def fake_get_sql_connection(connection_key: str) -> FakeDbapiConnection:
        assert connection_key == "gp"
        connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
        connections.append(connection)
        return connection

    def flaky_inspect_source_query_schema(*args: object, **kwargs: object) -> object:
        nonlocal inspection_calls
        inspection_calls += 1
        if inspection_calls == 1:
            raise RuntimeError("temporary schema inspection failure")
        return inspect_source_query_schema(*args, **kwargs)

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(
        create_module,
        "inspect_source_query_schema",
        flaky_inspect_source_query_schema,
    )

    result = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table",
        insert_data=False,
        retry_cnt=2,
        timeout_increment=0,
    )

    assert result is None
    assert inspection_calls == 2
    assert len(connections) == 2
    assert [connection.close_calls for connection in connections] == [1, 1]
    assert not any(sql.startswith("CREATE TABLE") for sql in connections[0].executed)
    assert any(
        sql.startswith("CREATE TABLE sandbox.target_table") for sql in connections[1].executed
    )


def test_create_table_from_sql_cleans_direct_insert_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[FakeDbapiConnection] = []
    target_exists = False
    create_calls = 0
    insert_calls = 0
    cleanup_calls = 0

    def fake_get_sql_connection(connection_key: str) -> FakeDbapiConnection:
        assert connection_key == "gp"
        connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
        connections.append(connection)
        return connection

    def fake_create(*args: object, **kwargs: object) -> None:
        nonlocal create_calls, target_exists
        create_calls += 1
        target_exists = True

    def flaky_insert(*args: object, **kwargs: object) -> int:
        nonlocal insert_calls
        insert_calls += 1
        if insert_calls == 1:
            raise RuntimeError("temporary insert failure")
        return 4

    def fake_drop(**kwargs: object) -> None:
        nonlocal cleanup_calls, target_exists
        cleanup_calls += 1
        target_exists = False

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(
        create_module,
        "table_exists",
        lambda *args, **kwargs: target_exists,
    )
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", fake_create)
    monkeypatch.setattr(create_module, "insert_from_query", flaky_insert)
    monkeypatch.setattr(create_module, "_drop_attempt_target", fake_drop)

    result = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table",
        retry_cnt=2,
        timeout_increment=0,
    )

    assert result == 4
    assert create_calls == 2
    assert insert_calls == 2
    assert cleanup_calls == 1
    assert target_exists is True
    assert len(connections) == 2
    assert [connection.close_calls for connection in connections] == [1, 1]


def test_create_table_from_sql_stops_retry_when_partial_target_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[FakeDbapiConnection] = []
    create_calls = 0
    insert_calls = 0
    cleanup_calls = 0

    def fake_get_sql_connection(connection_key: str) -> FakeDbapiConnection:
        assert connection_key == "gp"
        connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
        connections.append(connection)
        return connection

    def fake_create(*args: object, **kwargs: object) -> None:
        nonlocal create_calls
        create_calls += 1

    def fail_insert(*args: object, **kwargs: object) -> int:
        nonlocal insert_calls
        insert_calls += 1
        raise RuntimeError("original insert failure")

    def fail_cleanup(**kwargs: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise RuntimeError("cleanup failure")

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(create_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", fake_create)
    monkeypatch.setattr(create_module, "insert_from_query", fail_insert)
    monkeypatch.setattr(create_module, "_drop_attempt_target", fail_cleanup)

    with pytest.warns(RuntimeWarning, match="Could not remove partial target"):
        with pytest.raises(RuntimeError, match="original insert failure"):
            create_module.create_table_from_sql(
                "gp",
                "sandbox.target_table",
                "select id, amount from source_table",
                retry_cnt=3,
                timeout_increment=0,
            )

    assert create_calls == 1
    assert insert_calls == 1
    assert cleanup_calls == 2
    assert len(connections) == 2
    assert [connection.close_calls for connection in connections] == [1, 1]


def test_create_table_from_sql_cleans_cross_backend_transfer_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_connections: list[FakeDbapiConnection] = []
    target_connections: list[FakeClickHouseClient] = []
    transfer_calls: list[dict[str, object]] = []
    target_exists = False
    cleanup_calls = 0

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
            source_connections.append(connection)
            return connection
        if connection_key == "ch":
            connection = FakeClickHouseClient()
            target_connections.append(connection)
            return connection
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    def fake_create(*args: object, **kwargs: object) -> None:
        nonlocal target_exists
        target_exists = True

    def flaky_transfer(**kwargs: object) -> int:
        transfer_calls.append(kwargs)
        if len(transfer_calls) == 1:
            raise RuntimeError("temporary transfer failure")
        return 7

    def fake_drop(**kwargs: object) -> None:
        nonlocal cleanup_calls, target_exists
        cleanup_calls += 1
        target_exists = False

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(
        create_module,
        "table_exists",
        lambda *args, **kwargs: target_exists,
    )
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", fake_create)
    monkeypatch.setattr(create_module, "transfer_table", flaky_transfer)
    monkeypatch.setattr(create_module, "_drop_attempt_target", fake_drop)

    result = create_module.create_table_from_sql(
        "gp",
        "analytics.events",
        "select id, amount from source_table",
        table_db="ch",
        retry_cnt=2,
        timeout_increment=0,
    )

    assert result == 7
    assert cleanup_calls == 1
    assert target_exists is True
    assert len(transfer_calls) == 2
    assert all(call["retry_cnt"] == 1 for call in transfer_calls)
    assert all(call["full_retry_cnt"] == 1 for call in transfer_calls)
    assert len(source_connections) == 2
    assert len(target_connections) == 3
    assert all(connection.close_calls == 1 for connection in source_connections)
    assert all(connection.close_calls == 1 for connection in target_connections)


def test_create_table_from_sql_cleans_fast_path_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[FakeClickHouseClient] = []
    fast_path_calls = 0
    cleanup_calls = 0
    target_exists = False

    def fake_get_sql_connection(connection_key: str) -> FakeClickHouseClient:
        assert connection_key == "ch"
        connection = FakeClickHouseClient()
        connections.append(connection)
        return connection

    def flaky_fast_path(**kwargs: object) -> tuple[bool, object]:
        nonlocal fast_path_calls, target_exists
        fast_path_calls += 1
        target_exists = True
        if fast_path_calls == 1:
            raise RuntimeError("temporary ClickHouse insert failure")
        return True, "created"

    def fake_drop(**kwargs: object) -> None:
        nonlocal cleanup_calls, target_exists
        cleanup_calls += 1
        target_exists = False

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(
        create_module,
        "table_exists",
        lambda *args, **kwargs: target_exists,
    )
    monkeypatch.setattr(
        create_module.get_backend_adapter("ch"),
        "create_table_from_sql_fast_path",
        flaky_fast_path,
    )
    monkeypatch.setattr(create_module, "_drop_attempt_target", fake_drop)

    result = create_module.create_table_from_sql(
        "ch",
        "analytics.events",
        "select id, amount from source_table",
        retry_cnt=2,
        timeout_increment=0,
    )

    assert result == "created"
    assert fast_path_calls == 2
    assert cleanup_calls == 1
    assert target_exists is True
    assert len(connections) == 3
    assert all(connection.close_calls == 1 for connection in connections)


def test_create_table_from_sql_does_not_retry_or_drop_preexisting_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    create_calls = 0

    def fail_create(*args: object, **kwargs: object) -> None:
        nonlocal create_calls
        create_calls += 1
        raise RuntimeError("target already exists")

    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )
    monkeypatch.setattr(
        create_module,
        "table_exists",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", fail_create)
    monkeypatch.setattr(
        create_module,
        "_drop_attempt_target",
        lambda **kwargs: pytest.fail("pre-existing target must not be dropped"),
    )

    with pytest.raises(RuntimeError, match="target already exists"):
        create_module.create_table_from_sql(
            "gp",
            "sandbox.target_table",
            "select id, amount from source_table",
            retry_cnt=3,
            timeout_increment=0,
        )

    assert create_calls == 1
    assert connection.close_calls == 1


def test_create_table_from_sql_close_failure_does_not_mask_success_or_leak_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    target = CloseFailDbapiConnection()

    def fake_get_sql_connection(connection_key: str) -> FakeDbapiConnection:
        return source if connection_key == "gp" else target

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)

    with pytest.warns(RuntimeWarning, match="Could not close SQL connection 'trino'"):
        result = create_module.create_table_from_sql(
            "gp",
            "sandbox.target_table",
            "select id, amount from source_table",
            table_db="trino",
            insert_data=False,
            retry_cnt=2,
            timeout_increment=0,
        )

    assert result is None
    assert target.close_calls == 1
    assert source.close_calls == 1


def test_create_table_from_sql_stops_retry_when_partial_target_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[FakeDbapiConnection] = []
    create_calls = 0
    insert_calls = 0
    cleanup_calls = 0

    def fake_get_sql_connection(connection_key: str) -> FakeDbapiConnection:
        assert connection_key == "gp"
        connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
        connections.append(connection)
        return connection

    def fake_create(*args: object, **kwargs: object) -> None:
        nonlocal create_calls
        create_calls += 1

    def fail_insert(*args: object, **kwargs: object) -> int:
        nonlocal insert_calls
        insert_calls += 1
        raise RuntimeError("temporary insert failure")

    def fail_cleanup(**kwargs: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise RuntimeError("cleanup unavailable")

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(create_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", fake_create)
    monkeypatch.setattr(create_module, "insert_from_query", fail_insert)
    monkeypatch.setattr(create_module, "_drop_attempt_target", fail_cleanup)

    with pytest.warns(RuntimeWarning, match="Could not remove partial target"):
        with pytest.raises(RuntimeError, match="temporary insert failure"):
            create_module.create_table_from_sql(
                "gp",
                "sandbox.target_table",
                "select id, amount from source_table",
                retry_cnt=3,
                timeout_increment=0,
            )

    assert create_calls == 1
    assert insert_calls == 1
    assert cleanup_calls == 2
    assert len(connections) == 2
    assert [connection.close_calls for connection in connections] == [1, 1]


def test_clickhouse_fast_path_applies_only_to_same_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_module.get_backend_adapter("ch")
    assert ch_fast_path_module.uses_create_table_from_sql_fast_path(
        adapter,
        source_backend="ch",
        source_key="ch",
        target_key="ch",
    )
    assert not ch_fast_path_module.uses_create_table_from_sql_fast_path(
        adapter,
        source_backend="gp",
        source_key="gp",
        target_key="ch",
    )
    assert ch_fast_path_module.create_table_from_sql_fast_path(
        adapter,
        source_backend="gp",
        source_key="gp",
        target_key="ch",
        target_table="events",
        source_sql="SELECT 1",
        partition_by=None,
        order_by=None,
        ch_engine="MergeTree",
        ch_cluster="cluster",
        ch_sharding_key="rand()",
        ch_only_shard=False,
        ch_retry_per_host_drops=True,
        insert_data=True,
        drop_target_if_exists=False,
        dry_run=False,
        return_sql=False,
        query_label=None,
        return_metadata=False,
        table_schema=None,
    ) == (False, None)

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.sql.backends.ch.create_table_as"),
        "ch_create_table_as",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "created",
    )
    applied, result = ch_fast_path_module.create_table_from_sql_fast_path(
        adapter,
        source_backend="ch",
        source_key="ch",
        target_key="ch",
        target_table="events",
        source_sql="SELECT 1",
        partition_by=["dt"],
        order_by=["id"],
        ch_engine="MergeTree",
        ch_cluster="cluster",
        ch_sharding_key="id",
        ch_only_shard=True,
        ch_retry_per_host_drops=False,
        insert_data=False,
        drop_target_if_exists=True,
        dry_run=True,
        return_sql=False,
        query_label="job",
        return_metadata=True,
        table_schema={"id": "UInt64"},
    )
    assert (applied, result) == (True, "created")
    assert calls[0][0] == ("ch", "events", "SELECT 1")
    assert calls[0][1]["ch_only_shard"] is True


def test_create_table_from_sql_clickhouse_dry_fast_path_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_module.get_backend_adapter("ch")
    monkeypatch.setattr(
        adapter,
        "create_table_from_sql_fast_path",
        lambda **kwargs: (True, "fast plan"),
    )
    assert (
        create_module.create_table_from_sql(
            "ch",
            "events",
            "SELECT 1 AS id",
            dry_run=True,
        )
        == "fast plan"
    )


def _candidate_create_options(**overrides: object) -> Any:
    values: dict[str, object] = {
        "source_key": "gp",
        "source_backend": "gp",
        "target_key": "gp",
        "target_backend": "gp",
        "target_table": "sandbox.target",
        "source_sql": "SELECT id FROM source",
    }
    values.update(overrides)
    return create_module.CreateTableFromSqlOptions(**values)


def test_create_from_sql_compatibility_delegation_and_fast_unsafe_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transfer_api = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")
    monkeypatch.setattr(transfer_api, "transfer_table", lambda **kwargs: kwargs["to_table"])
    assert create_module.transfer_table(to_table="sandbox.target") == "sandbox.target"

    adapter = SimpleNamespace(
        create_table_from_sql_fast_path=lambda **_kwargs: (False, None),
    )
    monkeypatch.setattr(create_module, "get_sql_connection", lambda _key: pytest.fail("no probe"))
    monkeypatch.setattr(create_module, "_cleanup_attempt_target", lambda **_kwargs: False)
    failure = create_module._execute_create_table_from_sql_fast_path_attempt(
        options=_candidate_create_options(drop_target_if_exists=True),
        target_adapter=adapter,
        attempt=2,
    )
    assert isinstance(failure, create_module._UnsafeAttemptFailure)
    assert failure.attempt == 2


def test_create_from_sql_generic_metadata_direct_and_delegated_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[FakeDbapiConnection] = []

    def open_connection(_key: str) -> FakeDbapiConnection:
        connection = FakeDbapiConnection()
        connections.append(connection)
        return connection

    adapter = SimpleNamespace(
        validate_ch_columns_in_columns=lambda *_a, **_k: None,
        prepare_existing_target_for_create_from_sql=lambda *_a, **_k: False,
        build_create_from_sql_target_create_kwargs=lambda **_k: {},
        should_insert_create_table_from_sql_directly=lambda **_k: True,
    )
    monkeypatch.setattr(create_module, "get_sql_connection", open_connection)
    monkeypatch.setattr(
        create_module,
        "inspect_source_query_schema",
        lambda *_a, **_k: [SimpleNamespace(name="id")],
    )
    monkeypatch.setattr(create_module, "map_source_schema_to_target", lambda *_a: {"id": "BIGINT"})
    monkeypatch.setattr(create_module, "table_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(create_module, "insert_from_query", lambda *_a, **_k: 3)

    no_insert = create_module._execute_generic_create_table_from_sql_attempt(
        options=_candidate_create_options(insert_data=False, return_metadata=True),
        target_adapter=adapter,
        attempt=1,
    )
    assert no_insert.rows is None

    direct = create_module._execute_generic_create_table_from_sql_attempt(
        options=_candidate_create_options(return_metadata=True),
        target_adapter=adapter,
        attempt=1,
    )
    assert direct.rows == 3
    assert direct.metadata.source_rows == 3

    adapter.should_insert_create_table_from_sql_directly = lambda **_k: False
    transfer_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        create_module,
        "transfer_table",
        lambda **kwargs: transfer_calls.append(kwargs) or 4,
    )
    delegated = create_module._execute_generic_create_table_from_sql_attempt(
        options=_candidate_create_options(
            source_key="gp_source",
            target_key="trino_target",
            target_backend="trino",
            table_schema={"id": "BIGINT"},
            query_label="candidate-9",
            return_metadata=True,
            ch_only_shard=True,
        ),
        target_adapter=adapter,
        attempt=1,
    )
    assert delegated == 4
    assert transfer_calls[0]["query_label"] == "candidate-9"
    assert transfer_calls[0]["table_schema"] == {"id": "BIGINT"}
    assert transfer_calls[0]["return_metadata"] is True
    assert all(connection.close_calls == 1 for connection in connections)


def test_create_from_sql_cleanup_validation_bool_and_close_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="at least one column"):
        create_module._validate_source_columns([])
    with pytest.raises(ValueError, match="duplicate columns"):
        create_module._validate_source_columns(["id", "id", "value"])
    with pytest.raises(ValueError, match="boolean"):
        create_module._normalize_only_shard(1)

    options = _candidate_create_options(query_label="candidate-9")
    drop_calls: list[dict[str, object]] = []
    adapter = SimpleNamespace(
        rollback_quietly=lambda _connection: None,
        prepare_existing_target_for_create_from_sql=lambda *_a, **kwargs: drop_calls.append(kwargs),
    )
    create_module._drop_attempt_target(
        options=options,
        target_adapter=adapter,
        target_connection=object(),
    )
    assert drop_calls[0]["drop_target_if_exists"] is True

    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda _key: (_ for _ in ()).throw(RuntimeError("cleanup unavailable")),
    )
    with pytest.warns(RuntimeWarning, match="Could not remove partial target"):
        assert not create_module._cleanup_attempt_target(
            options=options,
            target_adapter=adapter,
            target_connection=None,
        )

    connection = FakeDbapiConnection()
    create_module._close_connections(
        source_connection=connection,
        source_key="gp",
        source_backend="gp",
        target_connection=connection,
        target_key="gp",
        target_backend="gp",
    )
    assert connection.close_calls == 1

    create_module._close_connections(
        source_connection=None,
        source_key="gp",
        source_backend="gp",
        target_connection=None,
        target_key="gp",
        target_backend="gp",
    )


def test_create_from_sql_delegated_failure_retains_unsafe_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        validate_ch_columns_in_columns=lambda *_a, **_k: None,
        prepare_existing_target_for_create_from_sql=lambda *_a, **_k: False,
        build_create_from_sql_target_create_kwargs=lambda **_k: {},
        should_insert_create_table_from_sql_directly=lambda **_k: False,
    )
    monkeypatch.setattr(create_module, "get_sql_connection", lambda _key: FakeDbapiConnection())
    monkeypatch.setattr(
        create_module,
        "inspect_source_query_schema",
        lambda *_a, **_k: [SimpleNamespace(name="id")],
    )
    monkeypatch.setattr(create_module, "map_source_schema_to_target", lambda *_a: {"id": "BIGINT"})
    monkeypatch.setattr(create_module, "table_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(
        create_module,
        "transfer_table",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("transfer failed")),
    )
    monkeypatch.setattr(create_module, "_cleanup_attempt_target", lambda **_kwargs: False)
    result = create_module._execute_generic_create_table_from_sql_attempt(
        options=_candidate_create_options(),
        target_adapter=adapter,
        attempt=3,
    )
    assert isinstance(result, create_module._UnsafeAttemptFailure)
    assert result.attempt == 3
