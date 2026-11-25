"""A CREATE acknowledgement cannot own a later table occupying the same name."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.load_column_state import ExistingColumnStore
from tests.sql._support.query_native_assignment import QueryNativeConnection, install_query_native


class PopulationError(PermissionError):
    analytics_toolkit_sql_retry_safe = False
    pgcode = "42501"


def test_public_query_cleanup_preserves_a_later_unowned_homonym_after_create_ack(
    monkeypatch, write_sql_connections
):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["gp"]["transfer_staging_schema"] = None
    write_sql_connections(configs)
    database, _aliases, connect = install_query_native(
        monkeypatch, [(1,), (2,)], native_type="BIGINT", exists=False
    )
    monkeypatch.setattr(
        get_backend_adapter("gp"),
        "open_connection",
        lambda config, **_kwargs: connect(config.connection_key),
    )
    original_source = deepcopy(database.tables[("sandbox", "source_rows")].rows)
    external = ExistingColumnStore("gp", ["payload"], not_null=())
    external.schema = {"payload": "BIGINT"}
    external.rows = [{"payload": value} for value in range(1000, 1100)]
    expected = deepcopy(external.rows)
    failure = PopulationError("Target population rejected")
    population = []
    changed = []
    insert = database._insert
    rollback = QueryNativeConnection.rollback

    def reject_population(tree, identity, params):
        if identity == ("sandbox", "events"):
            population.append(tree.copy())
            raise failure
        return insert(tree, identity, params)

    def replace_after_failed_statement_rollback(connection):
        rollback(connection)
        if population and not changed:
            acknowledged = database.tables[("sandbox", "events")]
            assert acknowledged is not external
            assert acknowledged.rows == []
            database.tables[("sandbox", "events")] = external
            changed.append(acknowledged)

    monkeypatch.setattr(database, "_insert", reject_population)
    monkeypatch.setattr(QueryNativeConnection, "rollback", replace_after_failed_statement_rollback)

    notice = pytest.warns(
        RuntimeWarning, match=r"Could not remove partial target.*ownership is unverified"
    )
    rejected = pytest.raises(PopulationError, match="Target population rejected")
    with notice, rejected as caught:
        sql.create_table(
            "gp",
            "sandbox.events",
            sql="SELECT payload FROM sandbox.source_rows",
            insert_data=True,
            retry_cnt=3,
            timeout_increment=0,
        )

    assert caught.value is failure
    assert len(population) == len(changed) == 1
    assert database.tables[("sandbox", "events")] is external
    assert external.rows == expected
    assert external.schema == {"payload": "BIGINT"}
    assert database.tables[("sandbox", "source_rows")].rows == original_source
    assert len(database.closed) == len(database.opened)
