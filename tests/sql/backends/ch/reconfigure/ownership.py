"""A facade never grants ownership of a same-named table in another database."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.plans import SqlPlan
from sqlglot import exp, parse_one

from tests.sql._support.fakes import FakeClickHouseResult
from tests.sql._support.reconfigure import ReconfigureClient, reconfigure_api


def table_sql(database, name):
    return exp.Table(this=exp.to_identifier(name), db=exp.to_identifier(database)).sql(
        dialect=ClickHouseDialect
    )


class OwnershipClient(ReconfigureClient):
    def __init__(self, facade_database, storage_database):
        super().__init__()
        self.facade_database = facade_database
        self.storage_database = storage_database
        self.facade = table_sql(facade_database, "events")
        self.storage = table_sql(storage_database, "events_shard")
        self.ddls = {
            (facade_database, "events"): (
                f"CREATE TABLE {self.facade} (id UInt64, payload String) "
                f"ENGINE=Distributed('core', '{storage_database}', 'events_shard', rand())"
            ),
            (storage_database, "events_shard"): (
                f"CREATE TABLE {self.storage} (id UInt64, payload String) "
                "ENGINE=MergeTree ORDER BY id"
            ),
        }
        self.rows = {
            (storage_database, "events_shard"): [
                {"id": 7, "payload": "source"},
                {"id": 8, "payload": "source"},
                {"id": 9, "payload": "source"},
            ]
        }
        if facade_database != storage_database:
            companion = table_sql(facade_database, "events_shard")
            self.ddls[(facade_database, "events_shard")] = (
                f"CREATE TABLE {companion} (id UInt64, payload String) ENGINE=MergeTree ORDER BY id"
            )
            self.rows[(facade_database, "events_shard")] = [
                {"id": 101, "payload": "unrelated"},
                {"id": 201, "payload": "unrelated"},
            ]
        self.ddl_reads = []

    def query(self, statement):
        if statement.startswith("SHOW CREATE TABLE "):
            table = parse_one(
                f"SELECT * FROM {statement.removeprefix('SHOW CREATE TABLE ')}",
                read=ClickHouseDialect,
            ).find(exp.Table)
            assert table is not None
            identity = table.db, table.name
            self.queries.append(statement)
            self.ddl_reads.append(identity)
            return FakeClickHouseResult([(self.ddls[identity],)])
        return super().query(statement)

    def command(self, statement, settings=None):
        super().command(statement, settings)
        message = "Reconfiguration submitted DDL before verifying full storage ownership."
        raise AssertionError(message)


@pytest.mark.parametrize("facade_database", ["a", "a.with.dot"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["execute", "plan"])
def test_public_reconfiguration_rejects_cross_database_storage_before_any_ddl(
    monkeypatch, facade_database, dry_run
):
    client = OwnershipClient(facade_database, "b")
    before = deepcopy(client.rows)
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _key: client)

    with pytest.raises(InvalidSqlInputError, match="not a managed Distributed/_shard pair"):
        sql.ch_reconfigure_table(
            "ch",
            client.facade,
            order_by="payload",
            ch_distributed_on_cluster="core",
            retry_cnt=1,
            timeout_increment=0,
            dry_run=dry_run,
        )

    assert client.commands == []
    assert client.rows == before
    assert client.rows[(facade_database, "events_shard")] == [
        {"id": 101, "payload": "unrelated"},
        {"id": 201, "payload": "unrelated"},
    ]
    assert client.ddl_reads == [(facade_database, "events")]
    assert client.closed is True


@pytest.mark.parametrize("database", ["a", "a.with.dot"])
def test_public_reconfiguration_keeps_same_database_managed_storage_identity(monkeypatch, database):
    client = OwnershipClient(database, database)
    before = deepcopy(client.rows)
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _key: client)

    plan = sql.ch_reconfigure_table(
        "ch",
        client.facade,
        order_by="payload",
        ch_distributed_on_cluster="core",
        retry_cnt=1,
        timeout_increment=0,
        dry_run=True,
    )

    assert isinstance(plan, SqlPlan)
    assert client.ddl_reads == [(database, "events"), (database, "events_shard")]
    assert any(statement.phase == "copy_data" for statement in plan.statements)
    assert any(statement.phase == "cutover" for statement in plan.statements)
    assert client.commands == []
    assert client.rows == before
    assert client.closed is True
