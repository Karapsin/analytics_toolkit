"""A native source stage survives and supplies a failed self-replacement retry."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit import sql

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.load_column_state import ExistingColumnStore
from tests.sql._support.query_session_namespace import install_session_query


def test_public_materialized_self_replacement_reuses_acknowledged_source_on_retry(
    monkeypatch, write_sql_connections
):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["gp"]["transfer_staging_schema"] = "scratch"
    write_sql_connections(configs)
    database = install_session_query(monkeypatch)
    original = ExistingColumnStore("gp", ["payload"], not_null=())
    original.schema = {"payload": "INTEGER"}
    original.rows = [{"payload": 1}, {"payload": 2}]
    database.tables[("archive", "events")] = original
    public_rows = deepcopy(database.tables[("public", "events")].rows)
    insert = database._insert
    publications = []

    def fail_first_population(tree, identity, params):
        if identity == ("archive", "events"):
            publications.append(tree.copy())
            if len(publications) == 1:
                message = "Retryable failure before target publication"
                raise ConnectionError(message)
        return insert(tree, identity, params)

    monkeypatch.setattr(database, "_insert", fail_first_population)
    result = sql.create_table(
        "gp",
        "archive.events",
        sql="SET search_path TO archive; SELECT payload FROM archive.events",
        insert_data=True,
        drop_if_exists=True,
        retry_cnt=2,
        timeout_increment=0,
    )

    assert result == 2
    assert database.tables[("archive", "events")].rows == [{"payload": 1}, {"payload": 2}]
    assert database.tables[("archive", "events")].schema == {"payload": "INT"}
    assert database.tables[("public", "events")].rows == public_rows
    assert len(publications) == 2
    assert database.selected_reads == 1
    # The script stage and its ordinal snapshot are separate, acknowledged
    # roles. Each is materialized once; retries use the certified replay spool.
    assert len(database.snapshot_creates) == 2
    assert len(set(database.snapshot_creates)) == 2
    assert len(database.setup_sessions) == 1
    assert set(database.tables) == {
        ("archive", "events"),
        ("archive", "source_rows"),
        ("public", "events"),
    }
    assert len(database.closed) == len(database.opened)
