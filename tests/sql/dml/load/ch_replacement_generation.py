"""Matching part statistics never authorize exchange of a later table generation."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError, InvalidSqlInputError
from sqlglot import exp

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_lifecycle import install_lifecycle
from tests.sql._support.ch_replacement_generation import install_recreated_target


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("role", ["physical", "facade"])
def test_public_replacement_retains_a_concurrently_recreated_target_generation(
    monkeypatch, write_sql_connections, driver, role
):
    state = install_recreated_target(monkeypatch, write_sql_connections, driver=driver, role=role)
    original = state.snapshot()
    frame = pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")})
    caller = frame.copy(deep=True)
    error = None

    try:
        sql.load_df(
            "ch",
            "analytics.events",
            frame,
            table_schema={"id": "UInt64"},
            write_mode="replace",
            retry_cnt=1,
            timeout_increment=0,
        )
    except AmbiguousSqlReplaceError as caught:
        error = caught

    assert state.recreated
    current = state.snapshot()
    changed = state.changed_snapshot
    assert {
        host: {name: current[host].get(name) for name in tables}
        for host, tables in original.items()
    } == {host: {name: changed[host][name] for name in tables} for host, tables in original.items()}
    assert isinstance(error, AmbiguousSqlReplaceError), repr(error)
    assert any(word in str(error).lower() for word in ("identity", "generation", "changed"))
    assert not any(
        strip_query_label(statement).startswith(("EXCHANGE ", "RENAME "))
        for statement in state.commands
    )
    host, name = state.offside
    assert state.rows(host, name) == [(201,), (202,)]
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
    pd.testing.assert_frame_equal(frame, caller)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_replacement_rejects_missing_original_generation_before_offside_create(
    monkeypatch, write_sql_connections, driver
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    before = state.snapshot()
    missing = []

    def missing_physical_uuid(tree, rows):
        if tuple(item.name for item in tree.expressions) != ("_shard_num", "uuid"):
            return rows
        selected = any(
            comparison.left.name == "name"
            and isinstance(comparison.right, exp.Literal)
            and comparison.right.this == "events_shard"
            for comparison in tree.find_all(exp.EQ)
        )
        staged = any(
            value.rows == [(201,), (202,)]
            for name, value in state.host_tables[state.coordinator].items()
            if name not in before[state.coordinator]
        )
        if selected and staged:
            missing.append(tree.copy())
            return []
        return rows

    state.metadata_transform = missing_physical_uuid
    frame = pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")})
    caller = frame.copy(deep=True)

    with pytest.raises(InvalidSqlInputError, match=r"original.*identity"):
        sql.load_df(
            "ch",
            "analytics.events",
            frame,
            table_schema={"id": "UInt64"},
            write_mode="replace",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert missing
    assert state.snapshot() == before
    assert not any("__replace_guarded_" in name for _host, name in state.created)
    assert not any(
        strip_query_label(statement).startswith(("EXCHANGE ", "RENAME "))
        for statement in state.commands
    )
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
    pd.testing.assert_frame_equal(frame, caller)
