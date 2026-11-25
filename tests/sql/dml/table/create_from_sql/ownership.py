"""Query creation must acquire each target before population or cleanup."""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.connection.errors import SqlTableReadinessError
from analytics_toolkit.sql.ddl.ownership import (
    CreationOwnership,
    acknowledge_created_resource,
    capture_creation_ownership,
    execute_owned_create_sqls,
)
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.create_from_sql import (
    SOURCE_DESCRIPTION,
    FakeDbapiConnection,
    FakeDbapiCursor,
    create_module,
)
from tests.sql._support.create_from_sql_protocol import (
    CatalogConnection,
    install_fake_type_registration,
)
from tests.sql._support.query_creation_ownership import (
    QueryOwnershipClient,
    install_query_ownership_client,
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


def _ch_client(monkeypatch, client, *, source=None):
    install_query_ownership_client(monkeypatch, client, source=source)


@pytest.mark.parametrize("same_alias", [False, True])
def test_query_create_readiness_failure_cleans_only_acknowledged_pair_resources(
    monkeypatch, same_alias
):
    install_fake_type_registration(monkeypatch)
    source = CatalogConnection(description=SOURCE_DESCRIPTION)
    client = QueryOwnershipClient()
    _ch_client(monkeypatch, client, source=source)
    before = client.state.snapshot()

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
    drops = [
        table
        for statement in client.commands
        if isinstance(
            (tree := parse_one(strip_query_label(statement), read=ClickHouseDialect)), exp.Drop
        )
        for table in tree.find_all(exp.Table)
    ]
    assert {table.name for table in drops} == {"events", "events_shard"}
    assert client.state.snapshot() == before
    assert not any(statement.startswith("INSERT INTO") for statement in client.commands)


@pytest.mark.parametrize("failure", ["collision", "lost_ack"])
def test_fast_query_create_retains_unowned_facade_and_never_deletes_it(monkeypatch, failure):
    client = QueryOwnershipClient(failure)
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
    assert client.fault["fired"]
    drops = [
        table
        for statement in client.commands
        if isinstance(
            (tree := parse_one(strip_query_label(statement), read=ClickHouseDialect)), exp.Drop
        )
        for table in tree.find_all(exp.Table)
    ]
    if failure == "collision":
        assert drops
        assert {table.name for table in drops} == {"events_shard"}
        for host, resource, identity, definition in client.fault["foreign"]:
            retained = client.state.host_tables[host]["analytics.events"]
            assert retained is resource
            assert retained.uuid == identity
            assert retained.create == definition
            assert retained.rows == [(number, Decimal(number)) for number in range(100)]
    else:
        assert drops == []
        assert any(
            "analytics.events_shard" in tables for tables in client.state.host_tables.values()
        )
        assert any("analytics.events" in tables for tables in client.state.host_tables.values())
    assert not any(statement.startswith("INSERT INTO") for statement in client.commands)


def test_explicit_query_replace_never_adopts_or_cleans_preexisting_resources(monkeypatch):
    client = QueryOwnershipClient("lost_ack")
    client.seed_existing_pair()
    before = client.state.snapshot()
    _ch_client(monkeypatch, client)
    preflights = []
    adapter = get_backend_adapter("ch")
    original_preflight = adapter.preflight_create_table_replacement

    def preflight(connection, options):
        preflights.append(options)
        return original_preflight(connection, options)

    monkeypatch.setattr(adapter, "preflight_create_table_replacement", preflight)
    with pytest.raises(ConnectionError) as error:
        sql.create_table(
            "ch",
            "analytics.events",
            sql="SELECT id, amount FROM source_table",
            ch_replace_table=True,
            ch_shard_on_cluster="storage",
            ch_distributed_on_cluster="core",
            ch_distributed_cluster="storage",
            insert_data=True,
            retry_cnt=3,
            timeout_increment=0,
        )
    assert len(preflights) == 1
    assert preflights[0].ch_replace_table
    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert error.value.analytics_toolkit_sql_acknowledged_resources == ()
    assert error.value.analytics_toolkit_sql_acknowledged_creates
    dropped = [
        table
        for statement in client.commands
        if isinstance(
            (tree := parse_one(strip_query_label(statement), read=ClickHouseDialect)), exp.Drop
        )
        for table in tree.find_all(exp.Table)
    ]
    assert len(dropped) == 1
    assert dropped[0].db == "analytics_toolkit_transfer"
    assert dropped[0].name.endswith("__source")
    assert {table.name for table in dropped}.isdisjoint({"events", "events_shard"})
    after = client.state.snapshot()
    coordinator, remote = client.state.coordinator, client.state.remote
    assert (
        after[coordinator]["analytics.events_shard"]
        == before[coordinator]["analytics.events_shard"]
    )
    assert (
        after[coordinator]["analytics.source_table"]
        == before[coordinator]["analytics.source_table"]
    )
    assert after[remote]["analytics.events"] == before[remote]["analytics.events"]


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
