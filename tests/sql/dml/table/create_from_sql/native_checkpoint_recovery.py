"""Uncertain live creation cannot dispose of the only native query checkpoint."""

from __future__ import annotations

import ipaddress

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml import query_native_assignment
from sqlglot import exp, parse_one

from tests.sql._support.query_native_assignment import install_query_native


def test_gp_self_source_lost_create_ack_retains_certified_native_checkpoints(monkeypatch):
    database, _databases, _connect = install_query_native(
        monkeypatch, [(["192.0.2.0/24", None],)], native_type="TEXT[]"
    )
    execute = database.execute
    lost_creates = []
    notices = []

    def lose_create_ack(connection, statement, params=None):
        tree = parse_one(database._ddl_schema_prefix(statement), read="postgres")
        result = execute(connection, statement, params)
        if isinstance(tree, exp.Create) and database.identity(tree.this) == ("sandbox", "events"):
            lost_creates.append(statement)
            message = "Lost target CREATE acknowledgement."
            raise ConnectionError(message)
        return result

    monkeypatch.setattr(database, "execute", lose_create_ack)
    monkeypatch.setattr(
        query_native_assignment, "time_print", lambda text, **_kwargs: notices.append(text)
    )

    with pytest.raises(ConnectionError, match="Lost target CREATE acknowledgement"):
        sql.create_table(
            "gp",
            "sandbox.events",
            sql="SELECT CAST(payload AS CIDR[]) AS payload FROM sandbox.events",
            insert_data=True,
            drop_if_exists=True,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert len(lost_creates) == len(database.source_evaluations) == 1
    assert ("sandbox", "events") not in database.tables
    checkpoints = {
        identity: table
        for identity, table in database.tables.items()
        if identity != ("sandbox", "source_rows")
    }
    assert len(checkpoints) == 2
    assert any(
        table.rows == [{"payload": [ipaddress.ip_network("192.0.2.0/24"), None]}]
        for table in checkpoints.values()
    )
    assert database.python_reads == database.bound_rows == []
    assert any(all(identity[1] in notice for identity in checkpoints) for notice in notices)
