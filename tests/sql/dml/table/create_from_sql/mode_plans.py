"""Public query-creation plans retain the selected collision and replacement modes."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from sqlparse import parse

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.create_from_sql import FakeDbapiConnection, create_module
from tests.sql._support.cross_area import ddl_create_table_module


def _local_clickhouse_policy(write_sql_connections):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["ch"]["ddl_defaults"]["regular"] = {
        "create_distributed_pair": False,
        "shard": {"engine": "MergeTree", "on_cluster": None},
    }
    write_sql_connections(configs)


@pytest.mark.parametrize("backend", ["gp", "ch"])
@pytest.mark.parametrize("if_not_exists", [False, True], ids=["error-on-existing", "skip-existing"])
def test_public_query_sql_generation_keeps_selected_existence_mode(
    monkeypatch, write_sql_connections, backend, if_not_exists
):
    if backend == "ch":
        _local_clickhouse_policy(write_sql_connections)
    connection = FakeDbapiConnection(description=[("id", 20, None, None, None, None, None)])
    opened = []

    def connect(alias):
        opened.append(alias)
        return connection

    monkeypatch.setattr(ddl_create_table_module, "get_sql_connection", connect)

    result = sql.create_table(
        backend,
        "sandbox.generated_events",
        source_db="gp",
        sql="SELECT 1 AS id",
        if_not_exists=if_not_exists,
        only_generate_sql=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert type(result) is str
    creates = [
        str(statement).strip() for statement in parse(result) if statement.get_type() == "CREATE"
    ]
    assert creates
    assert all(
        statement.upper().startswith("CREATE TABLE IF NOT EXISTS ") is if_not_exists
        for statement in creates
    )
    assert opened == ["gp"]
    assert connection.close_calls == 1
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in connection.executed)


@pytest.mark.parametrize("plan_option", ["dry_run", "return_sql"])
@pytest.mark.parametrize(
    ("if_not_exists", "replace"),
    [(False, False), (True, False), (False, True)],
    ids=["ordinary", "skip-existing", "replace-existing"],
)
def test_public_script_query_plan_keeps_existence_and_replacement_modes(
    monkeypatch, plan_option, if_not_exists, replace
):
    def unexpected_connection(*_args, **_kwargs):
        pytest.fail("Planning a query script must not open a database connection")

    monkeypatch.setattr(ddl_create_table_module, "get_sql_connection", unexpected_connection)

    plan = sql.create_table(
        "ch",
        "analytics.events",
        sql="SET max_threads = 1; SELECT 1 AS id",
        insert_data=True,
        if_not_exists=if_not_exists,
        ch_replace_table=replace,
        **{plan_option: True},
    )

    assert plan.options["if_not_exists"] is if_not_exists
    assert plan.options["skip_population_if_target_exists"] is if_not_exists
    assert plan.options["ch_replace_table"] is replace
    assert plan.options["table_schema"] is None
    assert plan.options["source_stage_name_is_placeholder"] is True
    phases = [statement.phase for statement in plan.statements]
    assert phases[0] == "setup"
    assert "materialize_source" in phases
    assert "cleanup_source_stage" in phases


def test_public_explicit_query_plan_uses_plain_clickhouse_create_for_default_mode(
    monkeypatch, write_sql_connections
):
    _local_clickhouse_policy(write_sql_connections)

    def unexpected_connection(*_args, **_kwargs):
        pytest.fail("Planning explicit query types must not open a database connection")

    monkeypatch.setattr(ddl_create_table_module, "get_sql_connection", unexpected_connection)

    plan = create_module.create_table_from_sql(
        "gp",
        "analytics.events",
        sql="SELECT 1 AS id",
        table_db="ch",
        table_schema={"id": "Int64"},
        insert_data=True,
        dry_run=True,
    )

    assert plan.options["if_not_exists"] is False
    creates = [statement.sql for statement in plan.statements if statement.phase == "create_target"]
    assert creates
    assert all("IF NOT EXISTS" not in statement.upper() for statement in creates)
