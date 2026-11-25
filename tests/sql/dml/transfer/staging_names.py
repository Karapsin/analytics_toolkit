"""Raw catalog components keep dots inside the discovered stage identifier."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.load_column_state import ExistingColumnStore
from tests.sql._support.query_native_assignment import install_query_native


def test_public_stale_cleanup_quotes_discovered_stage_dots_as_one_raw_component(
    monkeypatch, write_sql_connections
):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["gp"]["transfer_staging_schema"] = "scratch"
    write_sql_connections(configs)
    database, _aliases, connect = install_query_native(monkeypatch, [(1,)], native_type="BIGINT")
    monkeypatch.setattr(
        get_backend_adapter("gp"),
        "open_connection",
        lambda config, **_kwargs: connect(config.connection_key),
    )
    raw_name = "0123456789abcdef__a.b__stage"
    selected = ExistingColumnStore("gp", ["payload"], not_null=())
    selected.schema = {"payload": "BIGINT"}
    selected.rows = [{"payload": 7}]
    foreign = deepcopy(selected)
    foreign.rows = [{"payload": value} for value in range(100)]
    database.tables[("scratch", raw_name)] = selected
    database.tables[("0123456789abcdef__a", "b__stage")] = foreign
    before = deepcopy(foreign.rows)
    metadata = database.metadata

    def listing(statement, params):
        if "information_schema.tables" in statement and "table_name LIKE" in statement:
            assert params == ("scratch", "%")
            return [(raw_name,)]
        return metadata(statement, params)

    monkeypatch.setattr(database, "metadata", listing)
    assert sql.cleanup_stale_stage_tables("gp", clean_all=True, read_retry_cnt=1) is None

    assert ("scratch", raw_name) not in database.tables
    assert database.tables[("0123456789abcdef__a", "b__stage")].rows == before
    assert database.tables[("0123456789abcdef__a", "b__stage")].schema == foreign.schema
    assert len(database.closed) == len(database.opened)
