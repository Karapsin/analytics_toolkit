from __future__ import annotations

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import engine_name
from sqlglot import exp, parse_one

from tests.sql._support.create_from_sql import (
    SOURCE_DESCRIPTION,
    create_module,
    pytest,
)
from tests.sql._support.create_from_sql_protocol import (
    CatalogClickHouseClient,
    CatalogConnection,
    _without_distribution,
    install_fake_type_registration,
)
from tests.sql._support.transfer_driver_boundary import install_transfer_factories

FakeClickHouseClient = CatalogClickHouseClient
FakeDbapiConnection = CatalogConnection


@pytest.fixture(autouse=True)
def _offline_query_driver(monkeypatch):
    install_fake_type_registration(monkeypatch)

    def connect(key):
        return create_module.get_sql_connection(key)

    install_transfer_factories(monkeypatch, connect)


def _assert_native_columns(statement, expected, *, dialect="postgres"):
    tree = parse_one(
        _without_distribution(statement) if dialect == "postgres" else statement, read=dialect
    )
    assert isinstance(tree, exp.Create)
    actual = {
        column.name: column.args["kind"]
        for column in tree.this.expressions
        if isinstance(column, exp.ColumnDef)
    }
    assert actual == {
        name: exp.DataType.build(native, dialect=dialect) for name, native in expected.items()
    }
    return tree


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
        message = f"Unexpected connection key: {connection_key}"
        raise AssertionError(message)

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
        retry_cnt=1,
        timeout_increment=0,
        ch_ddl_ready_timeout_seconds=1,
    )

    assert result is None
    assert len(target.commands) == 4
    assert any(
        command.startswith("CREATE TABLE analytics.events_shard") for command in target.commands
    )
    assert not any(command.startswith("INSERT INTO ") for command in target.commands)
    assert source.close_calls == target.close_calls == 1


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
    target = CatalogClickHouseClient()

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        message = f"Unexpected connection key: {connection_key}"
        raise AssertionError(message)

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
        retry_cnt=1,
        timeout_increment=0,
        ch_ddl_ready_timeout_seconds=1,
    )

    assert not any(command.startswith("DROP TABLE") for command in target.commands)
    assert len(target.commands) == 4
    shard_sql, local_shard_sql, distributed_sql, local_distributed_sql = target.commands
    assert shard_sql.startswith("CREATE TABLE analytics.events_shard")
    assert "ON CLUSTER '{cluster}'" in shard_sql
    shard = _assert_native_columns(
        shard_sql,
        {"id": "Nullable(Int32)", "dt": "Nullable(Date)", "amount": "Nullable(Decimal(12, 2))"},
        dialect=ClickHouseDialect,
    )
    partition = shard.find(exp.PartitionedByProperty)
    assert partition is not None
    assert [column.name for column in partition.find_all(exp.Column)] == ["dt"]
    order = shard.find(exp.Order)
    assert order is not None
    assert [column.name for column in order.find_all(exp.Column)] == ["dt", "id"]
    # Each owned physical resource needs a plain CREATE acknowledgement;
    # IF NOT EXISTS would not establish ownership of a pre-existing companion.
    assert local_shard_sql.startswith("CREATE TABLE analytics.events_shard")
    local_shard = parse_one(local_shard_sql, read=ClickHouseDialect)
    assert not local_shard.args.get("exists")
    assert "ON CLUSTER" not in local_shard_sql
    assert distributed_sql.startswith("CREATE TABLE analytics.events")
    facade = parse_one(distributed_sql, read=ClickHouseDialect)
    assert engine_name(facade) == "Distributed"
    engine = facade.find(exp.EngineProperty).this
    assert engine.expressions[2] == exp.Literal.string("events_shard")
    sharding = engine.expressions[3]
    assert sharding == parse_one("cityHash64(id)", read=ClickHouseDialect)
    assert [column.name for column in sharding.find_all(exp.Column)] == ["id"]
    assert "ON CLUSTER" not in local_distributed_sql
    assert "EXISTS TABLE analytics.events" in target.state.queries
    assert "EXISTS TABLE analytics.events_shard" in target.state.queries
    assert any("clusterAllReplicas" in query for query in target.state.queries)
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
        message = f"Unexpected connection key: {connection_key}"
        raise AssertionError(message)

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)

    create_module.create_table_from_sql(
        "gp",
        "sandbox.created_table",
        "select id, name from source_table",
        table_db="trino",
        insert_data=False,
        drop_target_if_exists=True,
        retry_cnt=1,
        timeout_increment=0,
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
    # A complete integer domain proves the lazy delegation path. GP NUMERIC
    # also admits special values and deliberately requires complete profiling.
    source = FakeDbapiConnection(
        description=[SOURCE_DESCRIPTION[0], ("amount", 20, None, None, None, None)]
    )
    target = FakeClickHouseClient()
    transfer_calls: list[dict[str, object]] = []

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        message = f"Unexpected connection key: {connection_key}"
        raise AssertionError(message)

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
        retry_cnt=1,
        timeout_increment=0,
        ch_ddl_ready_timeout_seconds=1,
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
        command.startswith("CREATE TABLE analytics.events_shard") for command in target.commands
    )


def test_schema_only_creation_falls_back_for_gp_unbounded_numeric(
    monkeypatch,
) -> None:
    connection = CatalogConnection(
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
        retry_cnt=1,
        timeout_increment=0,
    )

    created = next(
        sql for sql in connection.executed if sql.startswith("CREATE TABLE sandbox.target_table")
    )
    _assert_native_columns(created, {"quantity": "NUMERIC"})
    assert connection.tables[("sandbox", "target_table")].native_creation_oid > 0


def test_schema_only_creation_preserves_gp_bytea_columns(monkeypatch) -> None:
    connection = CatalogConnection(
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
        retry_cnt=1,
        timeout_increment=0,
    )

    created = next(
        sql for sql in connection.executed if sql.startswith("CREATE TABLE sandbox.target_table")
    )
    _assert_native_columns(created, {"cheque_pk": "BYTEA", "quantity": "NUMERIC(12, 3)"})
    assert connection.tables[("sandbox", "target_table")].native_creation_oid > 0


def test_schema_only_creation_uses_native_metadata_types(monkeypatch) -> None:
    connection = CatalogConnection(description=SOURCE_DESCRIPTION)
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
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result is None
    assert connection.executed[0] == (
        "SELECT * FROM (select id, amount from source_table) AS source_schema_probe WHERE 1 = 0"
    )
    created = next(
        sql for sql in connection.executed if sql.startswith("CREATE TABLE sandbox.target_table")
    )
    _assert_native_columns(created, {"id": "INTEGER", "amount": "NUMERIC(12, 2)"})
    assert connection.tables[("sandbox", "target_table")].native_creation_oid > 0
    assert not any(sql.startswith("DROP TABLE") for sql in connection.executed)
    assert connection.close_calls == 1
