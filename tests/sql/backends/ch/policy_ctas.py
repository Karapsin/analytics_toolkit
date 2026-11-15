from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.creation_policy import ClickHouseCreationPolicy
from analytics_toolkit.sql.backends.ch.policy_ctas import create_table_as_with_policy
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql._support.create_from_sql import FakeDbapiConnection, ch_fast_path_module, importlib
from tests.sql._support.policy_ctas import Client

legacy = importlib.import_module("analytics_toolkit.sql.backends.ch.create_table_as")


def policy(pair: bool = False) -> ClickHouseCreationPolicy:
    return ClickHouseCreationPolicy(
        pair,
        "MergeTree",
        "storage" if pair else None,
        "Distributed({cluster}, {database}, {shard_table}, {sharding_key}, 'extra')",
        "storage" if pair else None,
        "facades" if pair else None,
        "id",
        1,
        ddl_wait_policy="wait_none",
    )


def options(**overrides):
    return dict(
        target_key="ch",
        target_table="analytics.events",
        source_sql="SELECT 1 AS id",
        partition_by=None,
        order_by=["id"],
        ch_only_shard=False,
        insert_data=True,
        drop_target_if_exists=False,
        dry_run=False,
        return_sql=False,
        query_label=None,
        return_metadata=True,
        table_schema={"id": "Int64"},
        **overrides,
    )


@pytest.mark.parametrize("pair", [False, True])
def test_resolved_policy_ctas_keeps_scopes_and_trailing_engine_arguments(monkeypatch, pair):
    client = Client()
    monkeypatch.setattr(legacy, "get_sql_connection", lambda _: client)
    monkeypatch.setattr(get_backend_adapter("ch"), "after_create_table", lambda *_a, **_k: None)
    result = create_table_as_with_policy(policy=policy(pair), **options())
    assert result.metadata.statement_count == len(client.commands)
    assert client.close_calls == 1
    creates = [sql for sql in client.commands if sql.startswith("CREATE")]
    assert all("CREATE TABLE IF NOT EXISTS" not in sql for sql in creates if "ON CLUSTER" in sql)
    if pair:
        assert "ON CLUSTER storage" in creates[0]
        assert "ON CLUSTER facades" in creates[2]
        assert "    'extra'" in creates[2]
    else:
        assert len(creates) == 1
        assert "events_shard" not in creates[0]
    assert client.commands[-1] == "INSERT INTO analytics.events\nSELECT 1 AS id"


@pytest.mark.parametrize("schema", [None, {"id": "Int64"}])
def test_policy_query_planning_opens_no_connections(monkeypatch, schema):
    monkeypatch.setattr(
        legacy, "get_sql_connection", lambda _: pytest.fail("dry run opened client")
    )
    args = options()
    args.update(dry_run=True, table_schema=schema, drop_target_if_exists=True, insert_data=False)
    plan = create_table_as_with_policy(policy=policy(), **args)
    assert plan.options["pending_existing_layout_verification"] is True
    assert bool(plan.options.get("pending_schema_inspection")) is (schema is None)
    assert all(item.phase != "insert_target" for item in plan.statements)


def test_policy_query_rejects_existing_target_before_population(monkeypatch):
    client = Client(exists=True)
    monkeypatch.setattr(legacy, "get_sql_connection", lambda _: client)
    with pytest.raises(InvalidSqlInputError, match="already exists"):
        create_table_as_with_policy(policy=policy(), **options())
    assert client.commands == []
    assert client.close_calls == 1


def test_policy_replacement_of_local_table_drops_only_verified_local_storage(monkeypatch):
    client = Client(exists=True, close_error=True)
    monkeypatch.setattr(legacy, "get_sql_connection", lambda _: client)
    monkeypatch.setattr(get_backend_adapter("ch"), "after_create_table", lambda *_a, **_k: None)
    args = options()
    args.update(drop_target_if_exists=True, table_schema=None, return_metadata=False)
    assert create_table_as_with_policy(policy=policy(), **args) is None
    assert [sql for sql in client.commands if sql.startswith("DROP")] == [
        "DROP TABLE IF EXISTS analytics.events",
    ]
    assert client.close_calls == 1


def test_replacement_mode_with_absent_target_never_drops_an_unverified_companion(monkeypatch):
    client = Client()
    monkeypatch.setattr(legacy, "get_sql_connection", lambda _: client)
    monkeypatch.setattr(get_backend_adapter("ch"), "after_create_table", lambda *_a, **_k: None)
    args = options()
    args.update(drop_target_if_exists=True, insert_data=False)
    result = create_table_as_with_policy(policy=policy(), **args)
    assert result.metadata.statement_count == 1
    assert all(not command.startswith("DROP") for command in client.commands)


def test_policy_query_insert_failure_keeps_primary_error_and_close_does_not_mask_it(monkeypatch):
    client = Client(close_error=True)
    error = ConnectionError("lost data acknowledgement")
    original = client.command

    def command(sql, **kwargs):
        if sql.startswith("INSERT"):
            raise error
        original(sql, **kwargs)

    client.command = command
    monkeypatch.setattr(legacy, "get_sql_connection", lambda _: client)
    monkeypatch.setattr(get_backend_adapter("ch"), "after_create_table", lambda *_a, **_k: None)
    with pytest.raises(ConnectionError) as caught:
        create_table_as_with_policy(policy=policy(), **options())
    assert caught.value is error
    assert client.close_calls == 1


def test_fast_path_forwards_the_resolved_policy(monkeypatch):
    called = []
    module = importlib.import_module("analytics_toolkit.sql.backends.ch.policy_ctas")
    monkeypatch.setattr(
        module, "create_table_as_with_policy", lambda **kwargs: called.append(kwargs)
    )
    args = options()
    args.update(
        source_backend="ch",
        source_key="ch",
        ch_engine="ignored",
        ch_cluster="ignored",
        ch_sharding_key="ignored",
        ch_retry_per_host_drops=False,
        ch_creation_policy=policy(),
    )
    applied, _ = ch_fast_path_module.create_table_from_sql_fast_path(
        get_backend_adapter("ch"), **args
    )
    assert applied is True
    assert called[0]["policy"] is args["ch_creation_policy"]


def test_legacy_fast_path_resolves_replace_policy_without_changing_drop_mode():
    args = options()
    args.update(
        source_backend="ch",
        source_key="ch",
        ch_engine="MergeTree",
        ch_cluster="core",
        ch_sharding_key="id",
        ch_only_shard=True,
        ch_retry_per_host_drops=False,
        ch_replace_table=True,
        dry_run=True,
        insert_data=False,
    )
    applied, plan = ch_fast_path_module.create_table_from_sql_fast_path(
        get_backend_adapter("ch"), **args
    )
    assert applied is True
    assert plan.options["ch_creation_policy"]["create_distributed_pair"] is False
    assert plan.options["ch_creation_policy"]["shard_on_cluster"] is None
    assert plan.options["ch_replace_table"] is True
    assert plan.options["drop_target_if_exists"] is False
    assert len(plan.statements) == 1
    assert plan.statements[0].sql.startswith("CREATE OR REPLACE TABLE analytics.events")


def test_failed_connection_acquisition_preserves_error_and_never_closes_unacquired_target(
    monkeypatch,
):
    module = importlib.import_module("analytics_toolkit.sql.backends.ch.policy_ctas")
    failure = ConnectionError("target could not be opened")

    def open_connection(_key):
        raise failure

    monkeypatch.setattr(legacy, "get_sql_connection", open_connection)
    monkeypatch.setattr(
        module, "close_connection_ref", lambda *_a: pytest.fail("no connection was acquired")
    )
    args = options()
    args["input_resolution"] = {}
    with pytest.raises(ConnectionError, match="target could not be opened") as caught:
        create_table_as_with_policy(policy=policy(), **args)
    assert caught.value is failure


@pytest.mark.parametrize("mode", ["default", "skip", "replace"])
def test_public_query_creation_existing_modes_never_accidentally_append(monkeypatch, mode):
    client = Client(exists=True)
    ddl = importlib.import_module("analytics_toolkit.sql.ddl.api")
    query = importlib.import_module("analytics_toolkit.sql.dml.table.create_table_from_sql")
    for module in (legacy, ddl, query):
        monkeypatch.setattr(module, "get_sql_connection", lambda _: client)
    monkeypatch.setattr(get_backend_adapter("ch"), "after_create_table", lambda *_a, **_k: None)
    arguments = {
        "sql": "SELECT 1 AS id",
        "insert_data": True,
        "ch_distributed_table": False,
        "ch_engine": "MergeTree",
        "ch_shard_on_cluster": "storage",
        "retry_cnt": 1,
    }
    if mode == "default":
        with pytest.raises(InvalidSqlInputError, match="already exists"):
            sql.create_table("ch", "analytics.events", **arguments)
        assert client.commands == []
    elif mode == "skip":
        assert sql.create_table("ch", "analytics.events", if_not_exists=True, **arguments) is None
        assert client.commands == []
    else:
        sql.create_table("ch", "analytics.events", ch_replace_table=True, **arguments)
        assert any(command.startswith("CREATE OR REPLACE TABLE") for command in client.commands)
        assert not any(command.startswith("DROP") for command in client.commands)
        assert not any("events_shard" in item for item in client.commands)
        assert any("ON CLUSTER storage" in item for item in client.commands)
        assert client.commands[-1] == "INSERT INTO analytics.events\nSELECT 1 AS id"


def test_public_query_policy_keeps_independent_ddl_and_routing_scopes(monkeypatch):
    client = Client()
    ddl = importlib.import_module("analytics_toolkit.sql.ddl.api")
    query = importlib.import_module("analytics_toolkit.sql.dml.table.create_table_from_sql")
    for module in (legacy, ddl, query):
        monkeypatch.setattr(module, "get_sql_connection", lambda _: client)
    monkeypatch.setattr(get_backend_adapter("ch"), "after_create_table", lambda *_a, **_k: None)
    sql.create_table(
        "ch",
        "analytics.events",
        sql="SELECT 1 AS id",
        insert_data=True,
        ch_engine="MergeTree",
        ch_distributed_table=True,
        ch_shard_on_cluster="storage",
        ch_distributed_on_cluster="facades",
        ch_distributed_cluster="routing",
        ch_sharding_key="id",
        ch_distributed_engine_template=(
            "Distributed({cluster}, {database}, {shard_table}, {sharding_key}, 'extra')"
        ),
        retry_cnt=1,
    )
    creates = [item for item in client.commands if item.startswith("CREATE")]
    assert "ON CLUSTER storage" in creates[0]
    assert "ON CLUSTER facades" in creates[2]
    assert "    'routing'" in creates[2]
    assert "    'extra'" in creates[2]


def test_public_schema_replace_rejects_unowned_companion_before_any_ddl(monkeypatch):
    class CompanionClient(Client):
        companion_rows = tuple(range(100))

        def metadata_definitions(self):
            return {
                **super().metadata_definitions(),
                ("analytics", "events_shard"): (
                    "CREATE TABLE analytics.events_shard (id Int64) ENGINE=MergeTree ORDER BY id"
                ),
            }

    client = CompanionClient(exists=True)
    ddl = importlib.import_module("analytics_toolkit.sql.ddl.api")
    monkeypatch.setattr(ddl, "get_sql_connection", lambda _: client)
    with pytest.raises(InvalidSqlInputError, match=r"companion.*already exists"):
        sql.create_table(
            "ch",
            "analytics.events",
            table_schema={"id": "Int64"},
            ch_replace_table=True,
            ch_distributed_table=True,
            ch_engine="MergeTree",
            ch_shard_on_cluster="core",
            ch_distributed_on_cluster="core",
            ch_distributed_cluster="core",
            retry_cnt=3,
            timeout_increment=0,
        )
    assert client.commands == []
    assert len(client.companion_rows) == 100
    assert client.close_calls == 1


def test_query_skip_retries_metadata_with_fresh_connections_and_never_populates(monkeypatch):
    error = ConnectionError("metadata acknowledgement lost")

    class FailedProbe(Client):
        def query(self, statement, settings=None):
            raise error

    first = FailedProbe(close_error=True)
    second = Client(exists=True, close_error=True)
    opened = []
    clients = iter((first, second))
    query_module = importlib.import_module("analytics_toolkit.sql.dml.table.create_table_from_sql")
    ddl = importlib.import_module("analytics_toolkit.sql.ddl.api")

    def open_connection(_alias):
        client = next(clients)
        opened.append(client)
        return client

    monkeypatch.setattr(query_module, "get_sql_connection", open_connection)
    monkeypatch.setattr(ddl, "get_sql_connection", lambda _: pytest.fail("redundant outer probe"))
    with pytest.warns(RuntimeWarning, match="Could not close SQL connection"):
        result = sql.create_table(
            "ch",
            "analytics.events",
            sql="SELECT 1 AS id",
            insert_data=True,
            if_not_exists=True,
            return_metadata=True,
            retry_cnt=2,
            timeout_increment=0,
        )
    assert result.rows is None
    assert result.metadata.statement_count == 0
    assert result.metadata.retry_attempts == 2
    assert opened == [first, second]
    assert first.close_calls == second.close_calls == 1
    assert first.commands == second.commands == []


@pytest.mark.parametrize("source_db", ["ch", "gp"])
@pytest.mark.parametrize("replace_table", [False, True])
def test_public_query_replace_flag_controls_sql_plan_without_implying_drop(
    source_db, replace_table
):
    plan = sql.create_table(
        "ch",
        "analytics.events",
        sql="SELECT 1 AS id",
        source_db=source_db,
        insert_data=True,
        ch_replace_table=replace_table,
        dry_run=True,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        ch_shard_on_cluster="storage",
    )
    creates = [statement for statement in plan.sqls if statement.startswith("CREATE")]
    expected = "CREATE OR REPLACE TABLE" if replace_table else "CREATE TABLE"
    assert creates
    assert all(statement.startswith(expected) for statement in creates)
    assert not any(statement.startswith("DROP") for statement in plan.sqls)
    assert plan.options["drop_target_if_exists"] is False


@pytest.mark.parametrize("replace_table", [False, True])
def test_query_sql_generation_preserves_replace_mode(monkeypatch, replace_table):
    source = FakeDbapiConnection(description=[("id", 23, None, None, None, None)])
    ddl = importlib.import_module("analytics_toolkit.sql.ddl.api")
    monkeypatch.setattr(ddl, "get_sql_connection", lambda alias: source)
    generated = sql.create_table(
        "ch",
        "analytics.events",
        sql="SELECT 1 AS id",
        source_db="gp",
        only_generate_sql=True,
        ch_replace_table=replace_table,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        ch_shard_on_cluster="storage",
    )
    expected = "CREATE OR REPLACE TABLE" if replace_table else "CREATE TABLE"
    assert generated.startswith(expected)
    assert "DROP TABLE" not in generated
    assert source.close_calls == 1
