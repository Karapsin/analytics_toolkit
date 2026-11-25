"""Replacing columns preserves permitted properties of the actual existing facade."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from sqlglot import exp

from tests.sql._support.ch_lifecycle import install_lifecycle


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize(
    ("properties", "property_type"),
    [
        ("COMMENT 'business  events'", exp.SchemaCommentProperty),
        ("SETTINGS fsync_after_insert=1, fsync_directories=1", exp.SettingsProperty),
    ],
    ids=["comment", "durability-settings"],
)
def test_public_replacement_keeps_existing_distributed_facade_properties(
    monkeypatch, write_sql_connections, driver, properties, property_type
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    state.host_tables[state.coordinator].pop("analytics.events")
    definition = parse_create_table(
        "CREATE TABLE analytics.events (id UInt64) "
        "ENGINE=Distributed('storage','analytics','events_shard',rand()) " + properties,
        "analytics.events",
    )
    state._create(state.coordinator, definition)
    original = state.snapshot()
    frame = pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")})
    caller = frame.copy(deep=True)

    result = sql.load_df(
        "ch",
        "analytics.events",
        frame,
        table_schema={"id": "UInt64"},
        write_mode="replace",
        return_metadata=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result.rows == result.metadata.final_target_rows == 2
    assert result.metadata.source_rows == result.metadata.staged_rows == 2
    facade = state.host_tables[state.coordinator]["analytics.events"]
    original_property = definition.find(property_type)
    assert original_property is not None
    assert facade.create.find(property_type) == original_property
    assert state.rows(state.remote, "analytics.events_shard") == [(201,), (202,)]
    assert (
        state.snapshot()[state.coordinator]["analytics.events_shard"]
        == original[state.coordinator]["analytics.events_shard"]
    )
    assert set(state.host_tables[state.coordinator]) == {
        "analytics.events",
        "analytics.events_shard",
    }
    assert set(state.host_tables[state.remote]) == {"analytics.events_shard"}
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
    pd.testing.assert_frame_equal(frame, caller)
