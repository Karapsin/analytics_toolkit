from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.creation_policy import ClickHouseCreationPolicy
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.ddl.api import _create_sql_table_with_connection
from analytics_toolkit.sql.ddl.ownership import (
    CreationOwnership,
    acknowledge_created_resource,
    capture_creation_ownership,
    execute_owned_create_sqls,
)

from tests.sql._support.fakes import FakeDbapiConnection


class Client:
    def __init__(self, error=None):
        self.commands = []
        self.error = error

    def command(self, sql, **_kwargs):
        self.commands.append(sql)
        if self.error is not None:
            raise self.error

    def query(self, sql, **_kwargs):
        # The cluster CREATE has a coordinator companion already. An IF NOT
        # EXISTS acknowledgement cannot acquire that existing local table.
        assert sql == "EXISTS TABLE db.physical"
        return SimpleNamespace(result_rows=[(1,)])


def test_clustered_create_acknowledges_owned_names_but_never_if_not_exists_collisions():
    client = Client()
    owned = []
    execute_owned_create_sqls(
        get_backend_adapter("ch"),
        client,
        [
            "CREATE TABLE db.physical ON CLUSTER core (id Int64) ENGINE=MergeTree ORDER BY id",
            "CREATE TABLE IF NOT EXISTS db.physical (id Int64) ENGINE=MergeTree ORDER BY id",
            "CREATE TABLE db.facade (id Int64) ENGINE=Distributed('core','db','physical',rand())",
        ],
        owned.append,
    )
    assert owned == ["db.physical", "db.facade"]
    assert len(client.commands) == 3


def test_dbapi_create_ownership_handles_create_as_and_non_create_statements():
    connection = FakeDbapiConnection()
    owned = []
    execute_owned_create_sqls(
        get_backend_adapter("gp"),
        connection,
        ["CREATE TABLE sandbox.events AS SELECT 1 AS id"],
        owned.append,
    )
    assert owned == ["sandbox.events"]
    assert connection.executed == ["CREATE TABLE sandbox.events AS SELECT 1 AS id"]
    with pytest.raises(InvalidSqlInputError, match="statically named CREATE TABLE"):
        execute_owned_create_sqls(
            get_backend_adapter("gp"),
            connection,
            ["CREATE TABLE sandbox.other AS SELECT 1 AS id", "SELECT 1"],
            owned.append,
        )
    assert owned == ["sandbox.events"]
    assert connection.executed == ["CREATE TABLE sandbox.events AS SELECT 1 AS id"]


def test_create_failure_never_claims_ownership_of_an_ambiguous_or_colliding_name():
    error = ConnectionError("lost CREATE acknowledgement")
    client = Client(error)
    owned = []
    with pytest.raises(ConnectionError) as caught:
        execute_owned_create_sqls(
            get_backend_adapter("ch"),
            client,
            ["CREATE TABLE db.events (id Int64) ENGINE=MergeTree ORDER BY id"],
            owned.append,
        )
    assert caught.value is error
    assert owned == []
    assert len(client.commands) == 1


def test_connection_create_reports_ownership_before_a_readiness_failure(monkeypatch):
    client = Client()
    owned = []
    failure = TimeoutError("created storage not ready")

    def wait(*_args, **_kwargs):
        assert owned == ["db.events"]
        raise failure

    monkeypatch.setattr(get_backend_adapter("ch"), "after_create_table", wait)
    policy = ClickHouseCreationPolicy(
        create_distributed_pair=False,
        shard_engine="MergeTree",
        shard_on_cluster=None,
        distributed_engine_template=None,
        distributed_cluster=None,
        distributed_on_cluster=None,
        sharding_key=None,
        ddl_ready_timeout_seconds=1,
    )
    with pytest.raises(TimeoutError, match="created storage not ready"):
        _create_sql_table_with_connection(
            "ch",
            client,
            "db.events",
            table_schema={"id": "Int64"},
            ch_creation_policy=policy,
            on_resource_created=owned.append,
        )
    assert owned == ["db.events"]
    assert client.commands[0].startswith("CREATE TABLE db.events")


@pytest.mark.parametrize(
    "statement", ["CREATE TABLE", "CREATE TABLE IF NOT EXISTS", "CREATE TABLE ?"]
)
def test_unverifiable_create_target_fails_before_submission(statement):
    connection = FakeDbapiConnection()
    owned = []
    with pytest.raises(InvalidSqlInputError, match="statically named"):
        execute_owned_create_sqls(get_backend_adapter("gp"), connection, [statement], owned.append)
    assert connection.executed == []
    assert owned == []


def test_creation_receipt_deduplicates_acknowledged_resource_identity():
    receipt = CreationOwnership(pending_resource="sandbox.events")
    with capture_creation_ownership(receipt):
        acknowledge_created_resource("sandbox.events")
        acknowledge_created_resource("sandbox.events")
    assert receipt.resources == ["sandbox.events"]
    assert receipt.pending_resource is None


def test_connection_creation_resolves_alias_policy_without_caller_policy():
    plan = _create_sql_table_with_connection(
        "ch",
        None,
        "sandbox.events",
        connection_key="ch",
        table_schema={"id": "Int64"},
        ch_only_shard=True,
        dry_run=True,
    )
    assert all("Distributed(" not in statement.sql for statement in plan.statements)
    assert any("MergeTree" in statement.sql for statement in plan.statements)
