"""Final native map-key identity also applies to composite key leaves."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.adaptive_transfer import install, require_streaming_fast_path, track_spools
from tests.sql._support.fakes import FakeDbapiConnection
from tests.sql._support.load_table import load_df_module

_CASES = [
    (
        "MAP(ROW(k VARCHAR(2)), VARCHAR)",
        "MAP(ROW(k CHAR(2)), VARCHAR)",
        {("b",): "safe"},
        {("a",): "first", ("a ",): "second"},
        {("a ",): "first", ("b",): "second"},
    ),
    (
        "MAP(ROW(k DOUBLE), VARCHAR)",
        "MAP(ROW(k REAL), VARCHAR)",
        {(1.0,): "safe"},
        {(16777216.0,): "first", (16777217.0,): "second"},
        {(16777216.0,): "first", (16777218.0,): "second"},
    ),
]


def existing_composite_transfer(monkeypatch, case, *, explicit_stage, collision):
    source, target, safe, bad, valid = case
    values = [safe, bad] if collision else [valid]
    scenario = install(
        monkeypatch,
        native_type=source,
        rows=[(value,) for value in values],
        from_db_key="trino",
        from_db_backend="trino",
        write_mode="truncate_insert",
        table_schema={"id": source} if explicit_stage else None,
    )
    scenario.existing_types = {"id": target}
    return scenario


@pytest.mark.parametrize("case", _CASES, ids=["row_char", "row_real"])
@pytest.mark.parametrize("explicit_stage", [False, True])
def test_composite_map_key_collision_stops_transfer_before_mutation(
    monkeypatch, case, explicit_stage
):
    scenario = existing_composite_transfer(
        monkeypatch, case, explicit_stage=explicit_stage, collision=True
    )
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


@pytest.mark.parametrize("case", _CASES, ids=["row_char", "row_real"])
def test_valid_composite_cast_preserves_stage_payload_and_existing_schema(monkeypatch, case):
    expected = dict(case[4])
    scenario = existing_composite_transfer(monkeypatch, case, explicit_stage=True, collision=False)
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == 1
    assert result.metadata.input_count == 1
    assert scenario.schemas == [{"id": case[0]}]
    assert scenario.existing_types == {"id": case[1]}
    assert scenario.loaded == [[(expected,)]]
    assert case[4] == expected
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert paths
    assert all(not path.exists() for path in paths)


def composite_load(monkeypatch, target, maps):
    mutations, loaded = [], []
    connection = FakeDbapiConnection()
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        load_df_module, "get_table_column_types", lambda *_args, **_kwargs: {"id": target}
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

    def insert(**kwargs):
        mutations.append("INSERT")
        loaded.append(kwargs["df"]["id"].tolist())
        return len(kwargs["df"])

    monkeypatch.setattr(load_df_module, "_load_dataframe", insert)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *_args, **_kwargs: None)

    def call():
        return sql.load_df(
            "trino",
            "sandbox.target",
            pd.DataFrame({"id": maps}),
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )

    return call, mutations, loaded


@pytest.mark.parametrize("case", _CASES, ids=["row_char", "row_real"])
def test_composite_map_key_collision_stops_load_before_truncate_create_or_insert(monkeypatch, case):
    call, mutations, loaded = composite_load(monkeypatch, case[1], [case[2], case[3]])

    with pytest.raises(ValueError, match=r"map keys|keys in the authoritative map schema"):
        call()

    assert mutations == []
    assert loaded == []


@pytest.mark.parametrize("case", _CASES, ids=["row_char", "row_real"])
def test_valid_composite_map_load_retains_both_values(monkeypatch, case):
    expected = dict(case[4])
    call, mutations, loaded = composite_load(monkeypatch, case[1], [case[4]])

    assert call() == 1

    assert "TRUNCATE" in mutations
    assert mutations.count("INSERT") == 1
    assert loaded == [[expected]]
    assert case[4] == expected


@pytest.mark.parametrize("target_key", ["ROW(k VARCHAR(2))", "ROW(k VARCHAR(4))"])
def test_safe_same_and_widened_composite_keys_keep_lazy_streaming(monkeypatch, target_key):
    values = {("a",): "first", ("a ",): "second"}
    scenario = install(
        monkeypatch,
        native_type="MAP(ROW(k VARCHAR(2)), VARCHAR)",
        rows=[(values,)],
        from_db_key="trino",
        from_db_backend="trino",
        write_mode="append",
        replace_target_table=False,
    )
    target = "MAP(" + target_key + ", VARCHAR)"
    scenario.existing_types = {"id": target}
    require_streaming_fast_path(monkeypatch)

    assert scenario.run() == 1

    assert scenario.schemas == [{"id": target}]
    assert scenario.loaded == [[(values,)]]
    assert scenario.read_count == 1
    assert scenario.events.index("create_target") < scenario.events.index(
        "complete:SELECT id FROM source"
    )


def time_map_transfer(monkeypatch, values, *, stage_type=None):
    scenario = install(
        monkeypatch,
        native_type="MAP(TIME(6) WITH TIME ZONE, VARCHAR)",
        rows=[(value,) for value in values],
        from_db_key="trino",
        from_db_backend="trino",
        write_mode="truncate_insert",
        table_schema={"id": stage_type} if stage_type is not None else None,
    )
    scenario.existing_types = {"id": "MAP(TIME(6), VARCHAR)"}
    return scenario


@pytest.mark.parametrize("explicit_stage", [False, True])
def test_frozen_time_map_last_row_cast_collision_rejects_before_any_target_stage(
    monkeypatch, explicit_stage
):
    safe = {dt.time(1, tzinfo=dt.timezone.utc): "safe"}
    collision = {
        dt.time(12, tzinfo=dt.timezone.utc): "first",
        dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=1))): "second",
    }
    assert len(collision) == 2
    stage_type = "MAP(TIME(6), VARCHAR)" if explicit_stage else None
    scenario = time_map_transfer(monkeypatch, [safe, collision], stage_type=stage_type)
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match="cannot be converted to the resolved destination schema"):
        scenario.run()

    assert scenario.read_count == 1
    assert "complete:SELECT id FROM source" in scenario.events
    assert "create_target" not in scenario.events
    assert scenario.schemas == []
    assert scenario.loaded == []
    assert scenario.finalizations == 0
    assert scenario.existing_types == {"id": "MAP(TIME(6), VARCHAR)"}
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("explicit_stage", [False, True])
def test_frozen_time_map_cast_keeps_sql_distinct_wall_keys_and_the_existing_schema(
    monkeypatch, explicit_stage
):
    source = {
        dt.time(12, 0, 0, 123400, tzinfo=dt.timezone.utc): "first",
        dt.time(13, 0, 0, 123499, tzinfo=dt.timezone(dt.timedelta(hours=1))): "second",
    }
    assert len(source) == 2
    stage_type = "MAP(TIME(3), VARCHAR)" if explicit_stage else None
    scenario = time_map_transfer(monkeypatch, [source], stage_type=stage_type)
    paths = track_spools(monkeypatch)
    first_fraction, second_fraction = (123000, 123000) if explicit_stage else (123400, 123499)
    expected = {
        dt.time(12, 0, 0, first_fraction): "first",
        dt.time(13, 0, 0, second_fraction): "second",
    }

    result = scenario.run(metadata=True)

    assert result.rows == 1
    assert result.metadata.input_count == 1
    assert scenario.schemas == [{"id": stage_type or "MAP(TIME(6), VARCHAR)"}]
    assert scenario.loaded == [[(expected,)]]
    assert len(scenario.loaded[0][0][0]) == 2
    assert len(source) == 2
    assert scenario.existing_types == {"id": "MAP(TIME(6), VARCHAR)"}
    assert scenario.read_count == 1
    assert scenario.finalizations == 1
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert paths
    assert all(not path.exists() for path in paths)
