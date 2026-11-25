"""Validate the actual structured text payload before bounded stage creation."""

from __future__ import annotations

import json

import pytest

from tests.sql._support.adaptive_transfer import install, track_spools

_EMPTY_ARRAY = '{"v":1,"backend":"ch","type":"Array(Int64)","value":["array",[]]}'


def _explicit_text_scenario(monkeypatch, backend, width, *, rows=None, native_type="Array(Int64)"):
    return install(
        monkeypatch,
        native_type=native_type,
        rows=rows if rows is not None else [([],)],
        to_db_key=backend,
        to_db_backend=backend,
        write_mode="append",
        replace_target_table=False,
        table_schema={"id": f"VARCHAR({width})"},
    )


def _assert_failed_before_target_creation(scenario, paths):
    assert scenario.read_count == 1
    assert f"complete:{scenario.options.source_sql}" in scenario.events
    assert "create_target" not in scenario.events
    assert not scenario.schemas
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_new_bounded_target_rejects_metadata_expanded_structured_text_before_creation(
    monkeypatch, backend
):
    scenario = _explicit_text_scenario(monkeypatch, backend, 60)
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match=r"(?i)(VARCHAR|resolved destination)"):
        scenario.run()

    _assert_failed_before_target_creation(scenario, paths)
    assert scenario.options.table_schema == {"id": "VARCHAR(60)"}


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_exact_structured_text_width_is_accepted_without_changing_schema(monkeypatch, backend):
    assert len(_EMPTY_ARRAY) == 65
    scenario = _explicit_text_scenario(monkeypatch, backend, 65)
    paths = track_spools(monkeypatch)

    assert scenario.run() == 1

    assert scenario.schemas == [{"id": "VARCHAR(65)"}]
    assert scenario.loaded == [[(_EMPTY_ARRAY,)]]
    assert scenario.read_count == 1
    assert scenario.events.index(f"complete:{scenario.options.source_sql}") < (
        scenario.events.index("create_target")
    )
    assert scenario.finalizations == 1
    assert paths
    assert all(not path.exists() for path in paths)


def test_last_unicode_array_value_is_validated_before_target_creation(monkeypatch):
    encoded = json.dumps(
        {
            "v": 1,
            "backend": "ch",
            "type": "Array(String)",
            "value": ["array", [["str", "é"], ["str", "雪"]]],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    scenario = _explicit_text_scenario(
        monkeypatch,
        "trino",
        len(encoded) - 1,
        rows=[([],), (["é", "雪"],)],
        native_type="Array(String)",
    )
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match=r"(?i)(VARCHAR|resolved destination)"):
        scenario.run()

    _assert_failed_before_target_creation(scenario, paths)


def test_nullable_native_array_text_keeps_sql_null_and_metadata_at_exact_width(monkeypatch):
    # GP native arrays can themselves be NULL; CH Array(Int64) cannot be nullable.
    encoded = '{"v":1,"backend":"gp","type":"integer[]","value":["array",[]]}'
    scenario = install(
        monkeypatch,
        native_type="integer[]",
        rows=[(None,), ([],), (None,)],
        from_db_key="gp",
        from_db_backend="gp",
        write_mode="append",
        replace_target_table=False,
        table_schema={"id": f"VARCHAR({len(encoded)})"},
    )
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == 3
    assert result.metadata.input_count == 3
    assert scenario.loaded == [[(None,), (encoded,), (None,)]]
    assert scenario.read_count == 1
    assert scenario.finalizations == 1
    assert paths
    assert all(not path.exists() for path in paths)


def test_explicit_text_stage_rejects_overflow_even_when_existing_target_is_wider(monkeypatch):
    scenario = _explicit_text_scenario(monkeypatch, "trino", 60)
    scenario.existing_types = {"id": "VARCHAR(200)"}
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match=r"(?i)(VARCHAR|resolved destination)"):
        scenario.run()

    _assert_failed_before_target_creation(scenario, paths)
    assert scenario.existing_types == {"id": "VARCHAR(200)"}


def test_fitting_text_stage_preserves_the_wider_existing_target_schema(monkeypatch):
    scenario = _explicit_text_scenario(monkeypatch, "trino", 65)
    scenario.existing_types = {"id": "VARCHAR(200)"}

    assert scenario.run() == 1

    assert scenario.loaded == [[(_EMPTY_ARRAY,)]]
    assert scenario.schemas == [{"id": "VARCHAR(65)"}]
    assert scenario.existing_types == {"id": "VARCHAR(200)"}
    assert scenario.finalizations == 1
