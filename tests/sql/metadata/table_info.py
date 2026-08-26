from __future__ import annotations

from tests.sql._support.cross_area import (
    Any,
    InspectableClickHouseClient,
    RoutingDbapiConnection,
    pytest,
    table_info_module,
)


def test_table_info_gp_reads_columns_and_skips_row_count_by_default(
    monkeypatch,
) -> None:
    def resolver(
        sql: str,
        params: tuple[Any, ...] | None,
    ) -> list[tuple[Any, ...]]:
        if sql == "SELECT to_regclass(%s)":
            assert params == ("sandbox.events",)
            return [("sandbox.events",)]
        if "information_schema.columns" in sql:
            assert params == ("sandbox", "events")
            return [
                ("id", "bigint", "int8", None, None),
                ("amount", "numeric", "numeric", 12, 2),
            ]
        if "COUNT" in sql:
            pytest.fail("count SQL should not run by default")
        return []

    connection = RoutingDbapiConnection(resolver)
    monkeypatch.setattr(
        table_info_module,
        "get_sql_connection",
        lambda key: connection,
    )

    info = table_info_module.table_info("gp", "sandbox.events")

    assert info.connection_key == "gp"
    assert info.backend == "gp"
    assert info.table == "sandbox.events"
    assert info.exists is True
    assert info.columns == {"id": "BIGINT", "amount": "NUMERIC(12, 2)"}
    assert info.row_count is None
    assert info.as_dict()["columns"] == info.columns
    frame = info.to_frame()
    assert frame["column_name"].tolist() == ["id", "amount"]
    assert frame["column_type"].tolist() == ["BIGINT", "NUMERIC(12, 2)"]
    assert connection.close_calls == 1


def test_table_info_row_count_when_requested(monkeypatch) -> None:
    def resolver(
        sql: str,
        params: tuple[Any, ...] | None,
    ) -> list[tuple[Any, ...]]:
        if sql == "SELECT to_regclass(%s)":
            return [("sandbox.events",)]
        if "information_schema.columns" in sql:
            return [("id", "bigint", "int8", None, None)]
        if sql == "SELECT COUNT(*) FROM sandbox.events":
            return [(42,)]
        return []

    connection = RoutingDbapiConnection(resolver)
    monkeypatch.setattr(
        table_info_module,
        "get_sql_connection",
        lambda key: connection,
    )

    info = table_info_module.table_info(
        "gp",
        "sandbox.events",
        include_row_count=True,
    )

    assert info.row_count == 42
    assert "SELECT COUNT(*) FROM sandbox.events" in connection.executed


def test_table_info_missing_table_skips_columns_and_row_count(monkeypatch) -> None:
    def resolver(
        sql: str,
        params: tuple[Any, ...] | None,
    ) -> list[tuple[Any, ...]]:
        if sql == "SELECT to_regclass(%s)":
            return []
        pytest.fail(f"unexpected SQL for missing table: {sql}")

    connection = RoutingDbapiConnection(resolver)
    monkeypatch.setattr(
        table_info_module,
        "get_sql_connection",
        lambda key: connection,
    )

    info = table_info_module.table_info(
        "gp",
        "sandbox.missing",
        include_row_count=True,
    )

    assert info.exists is False
    assert info.columns == {}
    assert info.row_count is None
    assert info.to_frame().iloc[0]["column_name"] is None


def test_table_info_trino_resolves_unqualified_and_schema_qualified_names(
    monkeypatch,
) -> None:
    def resolver(
        sql: str,
        params: tuple[Any, ...] | None,
    ) -> list[tuple[Any, ...]]:
        if "information_schema.tables" in sql:
            return [(1,)]
        if "information_schema.columns" in sql:
            return [("id", "bigint"), ("score", "double")]
        if sql == "SELECT COUNT(*) FROM iceberg.sandbox.events":
            return [(9,)]
        return []

    first_connection = RoutingDbapiConnection(resolver)
    second_connection = RoutingDbapiConnection(resolver)
    connections = iter([first_connection, second_connection])
    monkeypatch.setattr(
        table_info_module,
        "get_sql_connection",
        lambda key: next(connections),
    )

    unqualified = table_info_module.table_info(
        "trino",
        "events",
        include_row_count=True,
    )
    schema_qualified = table_info_module.table_info("trino", "mart.events")

    assert unqualified.resolved_table == "iceberg.sandbox.events"
    assert unqualified.row_count == 9
    assert first_connection.executed_params[:2] == [
        ("sandbox", "events"),
        ("sandbox", "events"),
    ]
    assert schema_qualified.resolved_table == "iceberg.mart.events"
    assert second_connection.executed_params[:2] == [
        ("mart", "events"),
        ("mart", "events"),
    ]


def test_table_info_clickhouse_includes_shard_table(monkeypatch) -> None:
    client = InspectableClickHouseClient()
    monkeypatch.setattr(table_info_module, "get_sql_connection", lambda key: client)

    info = table_info_module.table_info(
        "ch",
        "analytics.events",
        include_row_count=True,
    )

    assert info.exists is True
    assert info.columns == {"id": "UInt64", "name": "String"}
    assert info.row_count == 17
    assert info.shard_table == "analytics.events_shard"
    assert info.resolved_table is None
    assert client.queries == [
        "EXISTS TABLE analytics.events",
        "DESCRIBE TABLE analytics.events",
        "SELECT count() FROM analytics.events",
    ]
    assert client.close_calls == 1


def test_table_info_validates_boolean_and_blank_table_name() -> None:
    with pytest.raises(ValueError, match="include_row_count"):
        table_info_module.table_info("gp", "events", include_row_count=1)
    with pytest.raises(table_info_module.InvalidSqlInputError, match="Table name"):
        table_info_module.table_info("gp", "  ")

    info = table_info_module.SqlTableInfo(
        connection_key="gp",
        backend="gp",
        table="events",
        exists=False,
        columns={},
        row_count=None,
        resolved_table=None,
        shard_table=None,
    )
    assert info.to_frame().loc[0, "column_name"] is None
