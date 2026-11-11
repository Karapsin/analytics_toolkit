"""Converted map keys must remain distinct before any destination publication."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs

from tests.sql._support.adaptive_transfer import install, track_spools
from tests.sql._support.load_table import load_df_module

_UUID = "12345678-abcd-4321-abcd-123456789abc"
_CASE_KEYS = {_UUID.upper(): "first", _UUID: "second"}


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_transfer_rejects_map_keys_that_collide_after_authoritative_uuid_conversion(
    monkeypatch, existing, nested
):
    source_type = "Map(String, String)"
    target_type = "MAP(UUID, VARCHAR)"
    safe, collision = {_UUID: "safe"}, _CASE_KEYS
    if nested:
        source_type, target_type = "Array(" + source_type + ")", "ARRAY(" + target_type + ")"
        safe, collision = [safe], [collision]
    scenario = install(
        monkeypatch,
        native_type=source_type,
        rows=[(safe,), (collision,)],
        write_mode="truncate_insert" if existing else "append",
        table_schema=None if existing else {"id": target_type},
    )
    if existing:
        scenario.existing_types = {"id": target_type}
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match="cannot be converted to the resolved destination schema"):
        scenario.run()

    assert scenario.read_count == 1
    assert "complete:SELECT id FROM source" in scenario.events
    assert "create_target" not in scenario.events
    assert not scenario.schemas
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert paths
    assert all(not path.exists() for path in paths)


def test_inferred_transfer_keeps_distinct_string_keys_in_native_trino_map(monkeypatch):
    scenario = install(monkeypatch, native_type="Map(String, String)", rows=[(_CASE_KEYS,)])

    assert scenario.run() == 1

    assert scenario.schemas == [{"id": "MAP(VARCHAR, VARCHAR)"}]
    assert scenario.loaded == [[(_CASE_KEYS,)]]
    assert list(scenario.loaded[0][0][0].items()) == list(_CASE_KEYS.items())


def test_clickhouse_transfer_keeps_both_pairs_when_uuid_conversion_merges_keys(monkeypatch):
    scenario = install(
        monkeypatch,
        native_type="Map(String, String)",
        rows=[(_CASE_KEYS,)],
        to_db_key="ch",
        to_db_backend="ch",
        table_schema={"id": "Map(UUID, String)"},
    )

    assert scenario.run() == 1

    actual = scenario.loaded[0][0][0]
    assert isinstance(actual, OrderedMapPairs)
    assert list(actual.items()) == [(UUID(_UUID), "first"), (UUID(_UUID), "second")]
    assert scenario.read_count == 1


def test_inferred_transfer_uses_exact_typed_text_for_source_duplicate_uuid_keys(monkeypatch):
    pairs = OrderedMapPairs(((UUID(_UUID), "first"), (UUID(_UUID), "second")))
    scenario = install(monkeypatch, native_type="Map(UUID, String)", rows=[(pairs,)])

    assert scenario.run() == 1

    assert scenario.schemas == [{"id": "VARCHAR"}]
    actual = json.loads(scenario.loaded[0][0][0])
    assert actual["type"] == "Map(UUID, String)"
    assert actual["value"] == [
        "map_pairs",
        [
            [["uuid", _UUID], ["str", "first"]],
            [["uuid", _UUID], ["str", "second"]],
        ],
    ]
    assert scenario.read_count == 1


def test_load_existing_map_rejects_converted_uuid_key_collision_before_truncation(monkeypatch):
    mutations = []
    monkeypatch.setattr(
        load_df_module, "get_sql_connection", lambda _key: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *_args, **_kwargs: {"id": "MAP(UUID, VARCHAR)"},
    )
    monkeypatch.setattr(
        load_df_module,
        "_apply_load_target_write_mode",
        lambda *_args: mutations.append("TRUNCATE"),
    )
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *_args, **_kwargs: mutations.append("CREATE"),
    )
    monkeypatch.setattr(
        load_df_module,
        "_load_dataframe",
        lambda **_kwargs: mutations.append("INSERT") or 2,
    )
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *_args, **_kwargs: None)
    frame = pd.DataFrame({"id": [{_UUID: "safe"}, _CASE_KEYS]})

    with pytest.raises(
        ValueError, match="cannot preserve its keys in the authoritative map schema"
    ):
        sql.load_df(
            "trino",
            "sandbox.target",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert mutations == []


@pytest.mark.parametrize(
    ("source_type", "target_type", "safe", "collision"),
    [
        (
            "Map(Float64, String)",
            "MAP(REAL, VARCHAR)",
            {1.0: "safe"},
            {16777216.0: "first", 16777217.0: "second"},
        ),
        (
            "Map(String, String)",
            "MAP(CHAR(2), VARCHAR)",
            {"b": "safe"},
            {"a": "first", "a ": "second"},
        ),
    ],
)
def test_transfer_rejects_map_keys_that_collide_in_final_native_column_before_mutation(
    monkeypatch, source_type, target_type, safe, collision
):
    scenario = install(
        monkeypatch,
        native_type=source_type,
        rows=[(safe,), (collision,)],
        write_mode="truncate_insert",
    )
    scenario.existing_types = {"id": target_type}
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match="cannot be converted to the resolved destination schema"):
        scenario.run()

    assert scenario.read_count == 1
    assert "complete:SELECT id FROM source" in scenario.events
    assert "create_target" not in scenario.events
    assert not scenario.schemas
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert paths
    assert all(not path.exists() for path in paths)
