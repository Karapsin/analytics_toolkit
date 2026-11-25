"""Offline destructive loads defer unobserved ClickHouse resource scopes."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.load_table import load_df_module
from tests.sql.dml.load.client_validation import configure_client


@pytest.mark.parametrize("write_mode", ["replace", "truncate_insert"])
@pytest.mark.parametrize("flag", ["dry_run", "return_sql"])
def test_offline_load_plan_does_not_guess_clickhouse_destructive_ownership(
    monkeypatch, write_sql_connections, write_mode, flag
):
    configure_client(write_sql_connections, "http")
    opened = []

    def connect(_alias):
        opened.append(True)
        message = "Offline planning attempted to open a ClickHouse connection."
        raise AssertionError(message)

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    frame = pd.DataFrame({"id": [1]})
    original = frame.copy(deep=True)

    plan = sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        table_schema={"id": "Int64"},
        write_mode=write_mode,
        **{flag: True},
    )

    assert not any(step.phase in {"drop_target", "clear_target"} for step in plan.statements)
    assert plan.options["pending_existing_layout_verification"] is True
    assert plan.options["pending_resource_scope_resolution"] is True
    assert plan.options["execution_requires"]
    assert plan.metadata.statement_count is None
    assert opened == []
    pd.testing.assert_frame_equal(frame, original)
