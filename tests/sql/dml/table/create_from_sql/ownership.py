"""Query creation must acquire each target before population or cleanup."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.connection.errors import SqlTableReadinessError
from analytics_toolkit.sql.ddl.ownership import (
    CreationOwnership,
    acknowledge_created_resource,
    capture_creation_ownership,
    execute_owned_create_sqls,
)

from tests.sql._support.create_from_sql import (
    SOURCE_DESCRIPTION,
    FakeClickHouseClient,
    FakeDbapiConnection,
    FakeDbapiCursor,
    create_module,
)

legacy = importlib.import_module("analytics_toolkit.sql.backends.ch.create_table_as")


@pytest.mark.parametrize("failure", ["collision", "lost_ack"])
def test_generic_query_create_never_populates_or_deletes_unacknowledged_target(
    monkeypatch, failure
):
    class Cursor(FakeDbapiCursor):
        def execute(self, statement, params=None):
            super().execute(statement, params)
            if statement.startswith("CREATE TABLE "):
                if failure == "collision":
                    error = RuntimeError("target concurrently created")
                    error.pgcode = "42P07"
                    raise error
                message = "CREATE acknowledgement lost"
                raise ConnectionError(message)

    class Connection(FakeDbapiConnection):
        def cursor(self):
            cursor = Cursor(self)
            self.cursors.append(cursor)
            return cursor

    connection = Connection(description=SOURCE_DESCRIPTION)
    monkeypatch.setattr(create_module, "get_sql_connection", lambda _key: connection)
    expected = RuntimeError if failure == "collision" else ConnectionError
    with pytest.raises(expected) as error:
        sql.create_table(
            "gp",
            "sandbox.target",
            sql="SELECT id, amount FROM source_table",
            insert_data=True,
            retry_cnt=3,
            timeout_increment=0,
        )
    creates = [
        statement for statement in connection.executed if statement.startswith("CREATE TABLE ")
    ]
    assert len(creates) == 1
    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert "IF NOT EXISTS" not in creates[0]
    assert not any(
        statement.startswith(("DROP TABLE", "INSERT INTO")) for statement in connection.executed
    )


def _ch_client(monkeypatch, client):
    monkeypatch.setattr(create_module, "get_sql_connection", lambda _key: client)
    monkeypatch.setattr(legacy, "get_sql_connection", lambda _key: client)
    original_query = client.query

    def query(statement):
        if statement.startswith("DESCRIBE TABLE ("):
            return SimpleNamespace(result_rows=[("id", "UInt64"), ("amount", "Decimal(12, 2)")])
        if "_ch_create_table_as_source" in statement:
            return SimpleNamespace(
                column_names=["id", "amount"], column_types=["UInt64", "Decimal(12, 2)"]
            )
        return original_query(statement)

    client.query = query


@pytest.mark.parametrize("same_alias", [False, True])
def test_query_create_readiness_failure_cleans_only_acknowledged_pair_resources(
    monkeypatch, same_alias
):
    source = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    client = FakeClickHouseClient()
    _ch_client(monkeypatch, client)
    monkeypatch.setattr(
        create_module, "get_sql_connection", lambda key: client if key == "ch" else source
    )

    def not_ready(*_args, **_kwargs):
        message = "target readiness failed after CREATE acknowledgements"
        raise SqlTableReadinessError(message)

    monkeypatch.setattr(get_backend_adapter("ch"), "after_create_table", not_ready)
    with pytest.raises(SqlTableReadinessError):
        sql.create_table(
            "ch",
            "analytics.events",
            sql="SELECT id, amount FROM source_table",
            source_db="ch" if same_alias else "gp",
            insert_data=True,
            retry_cnt=1,
            timeout_increment=0,
        )
    drops = [statement for statement in client.commands if statement.startswith("DROP TABLE")]
    assert len(drops) == 2
    assert any("events_shard" in statement for statement in drops)
    assert any("events`" in statement for statement in drops)
    assert not any(statement.startswith("INSERT INTO") for statement in client.commands)


@pytest.mark.parametrize("failure", ["collision", "lost_ack"])
def test_fast_query_create_retains_unowned_facade_and_never_deletes_it(monkeypatch, failure):
    class Client(FakeClickHouseClient):
        def command(self, statement, settings=None):
            if statement.startswith("CREATE TABLE analytics.events\n"):
                self.commands.append(statement)
                self.created_tables.add("analytics.events")
                if failure == "collision":
                    error = RuntimeError("concurrent facade TABLE_ALREADY_EXISTS")
                    error.error_name = "TABLE_ALREADY_EXISTS"
                    raise error
                message = "facade CREATE acknowledgement lost"
                raise ConnectionError(message)
            return super().command(statement, settings=settings)

    client = Client()
    _ch_client(monkeypatch, client)
    expected = RuntimeError if failure == "collision" else ConnectionError
    with pytest.raises(expected):
        sql.create_table(
            "ch",
            "analytics.events",
            sql="SELECT id, amount FROM source_table",
            insert_data=True,
            retry_cnt=3,
            timeout_increment=0,
        )
    assert "analytics.events" in client.created_tables
    drops = [statement for statement in client.commands if statement.startswith("DROP TABLE")]
    if failure == "collision":
        assert len(drops) == 1
        assert "events_shard" in drops[0]
    else:
        assert drops == []
        assert "analytics.events_shard" in client.created_tables
    assert not any(statement.startswith("INSERT INTO") for statement in client.commands)


def test_explicit_query_replace_never_adopts_or_cleans_preexisting_resources(monkeypatch):
    class Client(FakeClickHouseClient):
        def command(self, statement, settings=None):
            if statement.startswith("CREATE OR REPLACE TABLE analytics.events\n"):
                self.commands.append(statement)
                message = "replacement facade acknowledgement lost"
                raise ConnectionError(message)
            return super().command(statement, settings=settings)

    client = Client()
    client.created_tables.update({"analytics.events", "analytics.events_shard"})
    _ch_client(monkeypatch, client)
    preflights = []
    monkeypatch.setattr(
        get_backend_adapter("ch"),
        "preflight_create_table_replacement",
        lambda _connection, options: preflights.append(options),
    )
    with pytest.raises(ConnectionError) as error:
        sql.create_table(
            "ch",
            "analytics.events",
            sql="SELECT id, amount FROM source_table",
            ch_replace_table=True,
            insert_data=True,
            retry_cnt=3,
            timeout_increment=0,
        )
    assert len(preflights) == 1
    assert preflights[0].ch_replace_table
    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert error.value.analytics_toolkit_sql_acknowledged_resources == ()
    assert error.value.analytics_toolkit_sql_acknowledged_creates
    assert not any(statement.startswith("DROP TABLE") for statement in client.commands)


def test_target_receipt_does_not_adopt_independently_owned_source_stage():
    receipt = CreationOwnership()
    source_resources = []
    adapter = get_backend_adapter("gp")
    connection = FakeDbapiConnection()
    with capture_creation_ownership(receipt):
        execute_owned_create_sqls(
            adapter,
            connection,
            ["CREATE TABLE source.snapshot (id BIGINT)"],
            source_resources.append,
        )
        assert receipt.resources == []
        assert not receipt.submitted
        execute_owned_create_sqls(
            adapter,
            connection,
            ["CREATE TABLE destination.target (id BIGINT)"],
            acknowledge_created_resource,
        )
    assert source_resources == ["source.snapshot"]
    assert receipt.resources == ["destination.target"]
