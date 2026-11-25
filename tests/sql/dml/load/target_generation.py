"""Failed new loads cannot clean a later object that reused their target name."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from sqlglot import exp

from tests.sql._support.ch_lifecycle import install_lifecycle


@pytest.mark.parametrize("transport", ["http", "native"])
def test_public_load_cleanup_keeps_a_recreated_target_generation(
    monkeypatch, write_sql_connections, transport
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=transport)
    initial = state.snapshot()
    physical_name = "analytics.new_events_shard"
    failure = ConnectionError("The publication connection failed before its data write.")
    replacement = []

    def open_publication(config, **kwargs):
        if (
            not replacement
            and physical_name in state.host_tables[state.remote]
            and "analytics.new_events" in state.host_tables[state.coordinator]
        ):
            original = state.host_tables[state.remote].pop(physical_name)
            definition = original.create.copy()
            properties = definition.args.get("properties")
            if properties is not None:
                properties.set(
                    "expressions",
                    [
                        item
                        for item in properties.expressions
                        if not isinstance(item, exp.UuidProperty)
                    ],
                )
            state._create(state.remote, definition, [(777,)])
            current = state.host_tables[state.remote][physical_name]
            assert current.uuid != original.uuid
            replacement.append((current, current.uuid, current.create.copy()))
            raise failure
        return state.open(config, **kwargs)

    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", open_publication)
    frame = pd.DataFrame({"id": [1]})
    original_frame = frame.copy(deep=True)

    with pytest.raises(ConnectionError) as raised:
        sql.load_df(
            "ch",
            "analytics.new_events",
            frame,
            table_schema={"id": "UInt64"},
            write_mode="append",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert raised.value is failure
    assert len(replacement) == 1
    original, identity, definition = replacement[0]
    retained = state.host_tables[state.remote][physical_name]
    assert retained is original
    assert retained.uuid == identity
    assert retained.create == definition
    assert retained.rows == [(777,)]
    assert any(
        "new_events_shard" in message and "storage" in message
        for message in raised.value.analytics_toolkit_sql_retained_resources
    )
    acknowledged = raised.value.analytics_toolkit_sql_acknowledged_scoped_resources
    assert any(
        "new_events_shard" in name and kind == "cluster" and cluster == "storage"
        for name, kind, cluster, _endpoint in acknowledged
    )
    for host, resources in initial.items():
        for table, snapshot in resources.items():
            assert state.snapshot()[host][table] == snapshot
    pd.testing.assert_frame_equal(frame, original_frame)
