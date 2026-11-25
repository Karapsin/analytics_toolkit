"""A transfer's CREATE acknowledgement cannot own a later stage homonym."""

from __future__ import annotations

from copy import deepcopy
from ipaddress import ip_network

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.dml.transfer.io.source import iter_source_batches
from psycopg2 import extensions

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.load_column_state import ExistingColumnStore
from tests.sql._support.query_native_assignment import (
    QueryNativeConnection,
    QueryNativeCursor,
    adaptive_input,
    attempt,
    install_query_native,
)


class FinalPopulationError(PermissionError):
    pgcode = "42501"
    analytics_toolkit_sql_retry_safe = False


@pytest.mark.parametrize("native_type", ["BIGINT", "CIDR"])
def test_public_transfer_stage_cleanup_preserves_a_later_unowned_homonym(
    monkeypatch, write_sql_connections, native_type
):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["gp"]["transfer_staging_schema"] = "scratch"
    configs["gp_sandbox"] = deepcopy(configs["gp"])
    write_sql_connections(configs)
    raw_values = [1, 2] if native_type == "BIGINT" else ["192.0.2.0/24", "198.51.100.0/24"]
    expected_values = raw_values if native_type == "BIGINT" else list(map(ip_network, raw_values))
    database, _aliases, connect = install_query_native(
        monkeypatch, [(value,) for value in raw_values], native_type=native_type
    )
    database.target.rows = [
        {"payload": 99 if native_type == "BIGINT" else ip_network("203.0.113.0/24")}
    ]
    before = deepcopy(database.target.rows)
    monkeypatch.setattr(
        get_backend_adapter("gp"),
        "open_connection",
        lambda config, **_kwargs: connect(config.connection_key),
    )

    def register_type(_caster, cursor):
        assert isinstance(cursor, QueryNativeCursor)

    monkeypatch.setattr(extensions, "register_type", register_type)
    monkeypatch.setattr(adaptive_input, "iter_source_batches", iter_source_batches)
    monkeypatch.setattr(attempt, "iter_source_batches", iter_source_batches)
    failure = FinalPopulationError("Final publication rejected")
    insert = database._insert
    rollback = QueryNativeConnection.rollback
    attempts = []
    replaced = []

    def fail_publication(tree, identity, params):
        if identity == ("sandbox", "events"):
            attempts.append(tree.copy())
            raise failure
        return insert(tree, identity, params)

    def external_recreation_after_rollback(connection):
        rollback(connection)
        if attempts and not replaced:
            identities = [identity for identity in database.tables if identity[0] == "scratch"]
            assert len(identities) == 1
            identity = identities[0]
            original = database.tables[identity]
            assert original.rows == [{"payload": value} for value in expected_values]
            foreign = ExistingColumnStore("gp", ["payload"], not_null=())
            foreign.schema = {"payload": native_type}
            foreign.rows = [
                {
                    "payload": value
                    if native_type == "BIGINT"
                    else ip_network(f"10.0.{value - 1000}.0/24")
                }
                for value in range(1000, 1100)
            ]
            database.tables[identity] = foreign
            replaced.append((identity, deepcopy(foreign.rows)))

    monkeypatch.setattr(database, "_insert", fail_publication)
    monkeypatch.setattr(QueryNativeConnection, "rollback", external_recreation_after_rollback)
    with pytest.raises(FinalPopulationError, match="Final publication rejected") as caught:
        sql.transfer(
            "gp",
            "gp_sandbox",
            from_sql="SELECT payload FROM sandbox.source_rows",
            to_table="sandbox.events",
            write_mode="append",
            ignore_source_staging=True,
            retry_cnt=1,
            full_retry_cnt=1,
            timeout_increment=0,
            full_timeout_increment=0,
        )

    assert caught.value is failure
    assert len(attempts) == len(replaced) == 1
    identity, rows = replaced[0]
    assert database.tables[identity].rows == rows
    assert database.tables[identity].schema == {"payload": native_type}
    assert database.target.rows == before
    assert len(database.source_evaluations) == 1
    assert len(database.closed) == len(database.opened)
