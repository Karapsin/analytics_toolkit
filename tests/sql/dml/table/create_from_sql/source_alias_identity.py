"""Public same-backend query creation honors source storage rather than alias spelling."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.query_native_assignment import QueryNativeDatabase, install_query_native


def _connections(write_sql_connections, *, same_database):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["gp"]["transfer_staging_schema"] = None
    configs["gp_sandbox"] = {
        **configs["gp"],
        "database": configs["gp"]["database"] if same_database else "independent_destination",
    }
    write_sql_connections(configs)


def test_public_cross_alias_self_replacement_freezes_before_destroying_shared_storage(
    monkeypatch, write_sql_connections
):
    _connections(write_sql_connections, same_database=True)
    database, _aliases, connect = install_query_native(
        monkeypatch, [(1,), (2,)], native_type="BIGINT"
    )
    monkeypatch.setattr(
        get_backend_adapter("gp"),
        "open_connection",
        lambda config, **_kwargs: connect(config.connection_key),
    )

    result = sql.create_table(
        "gp_sandbox",
        "sandbox.events",
        source_db="gp",
        sql="SELECT payload FROM sandbox.events",
        insert_data=True,
        drop_if_exists=True,
        return_metadata=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result.rows == 2
    assert database.target.rows == [{"payload": 1}, {"payload": 2}]
    assert database.target.schema == {"payload": "BIGINT"}
    assert database.python_selected_rows == [(1,), (2,)]
    assert len(database.python_reads) == 1
    assert set(database.tables) == {("sandbox", "events"), ("sandbox", "source_rows")}
    assert database.opened
    assert len(database.closed) == len(database.opened)


def test_public_query_from_independent_database_does_not_execute_against_target_alias(
    monkeypatch, write_sql_connections
):
    _connections(write_sql_connections, same_database=False)
    source, aliases, connect = install_query_native(monkeypatch, [(1,), (2,)], native_type="BIGINT")
    monkeypatch.setattr(
        get_backend_adapter("gp"),
        "open_connection",
        lambda config, **_kwargs: connect(config.connection_key),
    )
    target = QueryNativeDatabase([(91,), (92,)], native_type="BIGINT")
    aliases["gp_sandbox"] = target
    original_source = {
        name: (dict(table.schema), deepcopy(table.rows)) for name, table in source.tables.items()
    }

    result = sql.create_table(
        "gp_sandbox",
        "sandbox.events",
        source_db="gp",
        sql="SELECT payload FROM sandbox.source_rows",
        insert_data=True,
        drop_if_exists=True,
        return_metadata=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result.rows == 2
    assert target.target.rows == [{"payload": 1}, {"payload": 2}]
    assert {
        name: (dict(table.schema), table.rows) for name, table in source.tables.items()
    } == original_source
    assert target.tables[("sandbox", "source_rows")].rows == [{"payload": 91}, {"payload": 92}]
    assert len(source.python_reads) == 1
    assert source.opened
    assert target.opened
    assert len(source.closed) == len(source.opened)
    assert len(target.closed) == len(target.opened)
