"""Public loads publish the same complete payload that passed certification."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.load_column_state import existing_column_target


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize("mutation_phase", ["clear", "lost_data_ack"])
def test_load_keeps_certified_scalar_payload_when_the_caller_changes_during_io(
    monkeypatch, backend, mutation_phase
):
    store = existing_column_target(monkeypatch, backend, real_gp_adaptation=backend == "gp")
    store.schema = {"id": "SMALLINT"}
    frame = pd.DataFrame({"id": pd.Series([1], dtype=object)})
    writes = []
    changed = False
    execute = store.execute
    insert = store.insert

    def change_caller():
        nonlocal changed
        if not changed:
            changed = True
            frame.iloc[0, 0] = 70000

    def execute_boundary(statement, params=None):
        submissions = len(store.submissions)
        result = execute(statement, params)
        if (
            mutation_phase == "clear"
            and len(store.submissions) > submissions
            and store.submissions[-1][0] == "CLEAR"
        ):
            change_caller()
        return result

    def insert_boundary(names, rows):
        rows = list(rows)
        writes.extend(tuple(row) for row in rows)
        if any(not -(2**15) <= value < 2**15 for row in rows for value in row):
            message = "The destination rejected a value outside its SMALLINT domain."
            raise ValueError(message)
        insert(names, rows)
        if mutation_phase == "lost_data_ack" and not changed:
            change_caller()
            message = "The data write committed but its acknowledgement was lost."
            raise ConnectionError(message)

    monkeypatch.setattr(store, "execute", execute_boundary)
    monkeypatch.setattr(store, "insert", insert_boundary)

    result = sql.load_df(
        backend,
        "sandbox.events",
        frame,
        write_mode="truncate_insert",
        retry_cnt=2,
        timeout_increment=0,
    )

    assert result == 1
    assert isinstance(result, int)
    assert changed
    assert frame.iloc[0, 0] == 70000
    assert writes
    assert all(row == (1,) for row in writes)
    assert store.rows == [{"id": 1}]
