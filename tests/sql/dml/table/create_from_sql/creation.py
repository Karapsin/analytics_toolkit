from __future__ import annotations

from tests.sql._support.create_from_sql import (
    SOURCE_DESCRIPTION,
    FakeClickHouseClient,
    FakeDbapiConnection,
    create_module,
    pytest,
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
            "ch_sharding_key": "rand()",
            "ch_shard_on_cluster": "{cluster}",
            "ch_distributed_on_cluster": "{cluster}",
            "ch_distributed_cluster": "{cluster}",
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
