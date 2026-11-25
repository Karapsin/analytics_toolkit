"""A concurrent table cannot become disposable ownership after an absence probe."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.load_client_validation import ClientConnection, client_target
from tests.sql._support.load_table import load_df_module
from tests.sql.dml.load.client_validation import configure_client


class _ConcurrentTableExistsError(RuntimeError):
    code = 57
    error_name = "TABLE_ALREADY_EXISTS"


@pytest.mark.parametrize("transport", ["http", "native"])
def test_public_load_cannot_adopt_and_drop_a_concurrent_new_target(
    monkeypatch, write_sql_connections, transport
):
    configure_client(write_sql_connections, transport)
    store = client_target(monkeypatch, transport, {"payload": "Int64"}, exists=False)
    foreign_rows = [{"payload": 777}]
    foreign_schema = {"payload": "Int64"}
    foreign_key = ("sandbox", "events")
    creates = []
    raced = False
    open_failed = False
    execute = store.execute

    def execute_boundary(statement, params=None):
        nonlocal raced
        tree = parse_one(statement, read=ClickHouseDialect)
        if isinstance(tree, exp.Create):
            assert isinstance(tree.this, exp.Schema)
            table = tree.this.this
            if (table.db, table.name) == foreign_key:
                creates.append(statement)
                if not raced:
                    raced = True
                    store.schemas[foreign_key] = dict(foreign_schema)
                    store.table_rows[foreign_key] = deepcopy(foreign_rows)
                    foreign_ddl = tree.copy()
                    foreign_ddl.set("exists", False)
                    store.ddls[foreign_key] = foreign_ddl.sql(dialect=ClickHouseDialect)
                    store._select_target(foreign_key)
                if not tree.args.get("exists"):
                    message = "The concurrent table already exists."
                    raise _ConcurrentTableExistsError(message)
        return execute(statement, params)

    def connect(_alias):
        nonlocal open_failed
        if raced and not open_failed:
            open_failed = True
            message = "The insertion connection could not be opened."
            raise ConnectionError(message)
        return ClientConnection(store)

    monkeypatch.setattr(store, "execute", execute_boundary)
    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    frame = pd.DataFrame({"payload": [1]})
    original = frame.copy(deep=True)

    with pytest.raises((ConnectionError, _ConcurrentTableExistsError)):
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            table_schema=foreign_schema,
            write_mode="append",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert raced
    assert len(creates) == 1
    assert store.schemas[foreign_key] == foreign_schema
    assert store.table_rows[foreign_key] == foreign_rows
    assert store.bound_rows == []
    assert not any(kind == "DROP" for kind, _statement in store.submissions)
    pd.testing.assert_frame_equal(frame, original)
