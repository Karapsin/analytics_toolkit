"""Query-created storage keys are validated before deleting an existing target."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql._support.ch_lifecycle import install_lifecycle


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("option", ["partition_by", "order_by"])
def test_public_query_creation_rejects_missing_storage_key_before_drop(
    monkeypatch, write_sql_connections, driver, option
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    original = state.snapshot()
    with pytest.raises(InvalidSqlInputError) as caught:
        sql.create_table(
            "ch",
            "analytics.events",
            sql="SELECT CAST(201 AS UInt64) AS id",
            insert_data=False,
            drop_if_exists=True,
            ch_engine="MergeTree",
            retry_cnt=1,
            timeout_increment=0,
            **{option: ["missing_column"]},
        )
    assert state.snapshot() == original
    failure = caught.value
    assert option in str(failure)
    assert "missing_column" in str(failure)
    assert state.commands == []
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
