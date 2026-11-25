"""Scripted creation checks and mutates the namespace selected by native setup."""

from __future__ import annotations

from copy import deepcopy
from ipaddress import ip_network

from analytics_toolkit import sql

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.query_session_namespace import install_session_query


def test_public_script_skip_probe_uses_namespace_selected_by_source_setup(
    monkeypatch, write_sql_connections
):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["gp"]["transfer_staging_schema"] = "scratch"
    write_sql_connections(configs)
    database = install_session_query(monkeypatch)
    public = database.tables[("public", "events")]
    before = deepcopy(public.rows)

    result = sql.create_table(
        "gp",
        "events",
        sql="SET search_path TO archive; SELECT payload FROM archive.source_rows",
        insert_data=True,
        if_not_exists=True,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == 2
    assert database.tables[("archive", "events")].rows == [{"payload": 1}, {"payload": 2}]
    assert database.tables[("archive", "events")].schema == {"payload": "BIGINT"}
    assert database.tables[("public", "events")] is public
    assert public.rows == before
    assert len(database.setup_sessions) == database.selected_reads == 1
    assert set(database.tables) == {
        ("archive", "events"),
        ("archive", "source_rows"),
        ("public", "events"),
    }
    assert len(database.closed) == len(database.opened)


def test_public_certified_script_retry_keeps_the_setup_selected_target_namespace(
    monkeypatch, write_sql_connections
):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["gp"]["transfer_staging_schema"] = "scratch"
    write_sql_connections(configs)
    database = install_session_query(monkeypatch)
    source = database.tables[("archive", "source_rows")]
    source.schema = {"payload": "CIDR"}
    source.rows = [{"payload": ip_network(value)} for value in ("192.0.2.0/24", "198.51.100.0/24")]
    expected = deepcopy(source.rows)
    database.tables[("archive", "events")] = deepcopy(source)
    public = database.tables[("public", "events")]
    before = deepcopy(public.rows)
    database.next_relation_oid += 1
    public.native_creation_oid = database.next_relation_oid
    public_oid = public.native_creation_oid
    insert = database._insert
    attempts = []

    def fail_first_archive_population(tree, identity, params):
        if identity in {("archive", "events"), ("public", "events")}:
            attempts.append(identity)
            if len(attempts) == 1:
                message = "Retryable native checkpoint publication failure"
                raise ConnectionError(message)
        return insert(tree, identity, params)

    monkeypatch.setattr(database, "_insert", fail_first_archive_population)
    result = sql.create_table(
        "gp",
        "events",
        sql="SET search_path TO archive; SELECT payload FROM archive.source_rows",
        insert_data=True,
        drop_if_exists=True,
        retry_cnt=2,
        timeout_increment=0,
    )

    assert result == 2
    retained_public = database.tables[("public", "events")]
    assert retained_public.rows == before
    assert retained_public.native_creation_oid == public_oid
    assert retained_public.schema == {"payload": "BIGINT"}
    assert database.tables[("archive", "events")].rows == expected
    assert database.tables[("archive", "events")].schema == {"payload": "cidr"}
    assert attempts == [("archive", "events"), ("archive", "events")]
    assert len(database.setup_sessions) == 1
    assert database.selected_reads == 1
    assert set(database.tables) == {
        ("archive", "events"),
        ("archive", "source_rows"),
        ("public", "events"),
    }
    assert len(database.closed) == len(database.opened)
