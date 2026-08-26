from __future__ import annotations

from tests.sql._support.create_from_sql import (
    SOURCE_DESCRIPTION,
    FakeClickHouseClient,
    FakeDbapiConnection,
    create_module,
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
