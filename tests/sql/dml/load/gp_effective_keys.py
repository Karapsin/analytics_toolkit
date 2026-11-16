"""Keyed append compares native stored keys after supported text conversions."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.execution.plans import SqlPlan

from tests.sql._support.gp_effective_keys import effective_key_target

_ALIASES = [
    ("BIGINT", ["01", "1"], [1, 1]),
    ("BOOLEAN", ["false", "f"], [False, False]),
]


def _load(frame, native_type, *, explicit, keyed=True):
    options = {"table_schema": {"payload": native_type}} if explicit else {}
    return sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        write_mode="append",
        key_columns="payload" if keyed else None,
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(("native_type", "values", "converted"), _ALIASES)
def test_gp_effective_keys_reject_converted_aliases_before_target_append(
    monkeypatch, explicit, native_type, values, converted
):
    database = effective_key_target(monkeypatch, native_type)
    original_target = deepcopy(database.target)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)
    assert not frame.duplicated().any()

    with pytest.raises(ValueError, match=r"Duplicate key values.*staged data"):
        _load(frame, native_type, explicit=explicit)

    assert database.target.schema == original_target.schema
    assert database.target.rows == original_target.rows
    assert database.live_mutations == []
    assert database.bound_rows == [(value,) for value in converted]
    assert database.staged_rows == [
        ({"payload": native_type}, [{"payload": value} for value in converted])
    ]
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    ("native_type", "values", "converted"),
    [("BIGINT", ["01", "2"], [1, 2]), ("BOOLEAN", ["false", "t"], [False, True])],
)
def test_gp_effective_keys_keep_native_distinct_keys_and_bare_row_count(
    monkeypatch, explicit, native_type, values, converted
):
    database = effective_key_target(monkeypatch, native_type, empty=True)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    result = _load(frame, native_type, explicit=explicit)

    assert result == 2
    assert isinstance(result, int)
    assert database.target.schema == {"payload": native_type}
    assert database.target.rows == [{"payload": value} for value in converted]
    assert database.bound_rows == [(value,) for value in converted]
    assert database.staged_rows == [
        ({"payload": native_type}, [{"payload": value} for value in converted])
    ]
    assert len(database.live_mutations) == 1
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize(("native_type", "values", "converted"), _ALIASES)
def test_gp_effective_keys_leave_ordinary_append_duplicate_semantics_unchanged(
    monkeypatch, native_type, values, converted
):
    database = effective_key_target(monkeypatch, native_type)
    seed = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, native_type, explicit=False, keyed=False) == 2

    assert database.target.rows == seed + [{"payload": value} for value in converted]
    assert database.bound_rows == [(value,) for value in converted]
    assert database.staged_rows == []
    assert len(database.live_mutations) == 1
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize(
    ("physical", "requested", "values", "stored"),
    [
        ("SMALLINT", "BIGINT", ["01", "2"], [1, 2]),
        ("CHARACTER(2)", "VARCHAR(2)", ["a", "b"], ["a ", "b "]),
    ],
    ids=["integer-width-assignment", "character-assignment"],
)
def test_gp_effective_keys_keep_distinct_values_across_native_schema_assignment(
    monkeypatch, physical, requested, values, stored
):
    database = effective_key_target(monkeypatch, physical, empty=True)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, requested, explicit=True) == 2

    assert database.target.schema == {"payload": physical}
    assert database.target.rows == [{"payload": value} for value in stored]
    assert len(database.bound_rows) == 2
    assert len(database.live_mutations) == 1
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in database.opened)


def test_gp_effective_keys_reject_final_native_character_assignment_collision(monkeypatch):
    database = effective_key_target(monkeypatch, "CHARACTER(2)")
    seed = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": ["a", "a "]})
    original = frame.copy(deep=True)
    assert not frame.duplicated().any()

    with pytest.raises(ValueError, match=r"Duplicate.*key"):
        _load(frame, "VARCHAR(2)", explicit=True)

    assert database.target.schema == {"payload": "CHARACTER(2)"}
    assert database.target.rows == seed
    assert database.live_mutations == []
    assert database.bound_rows == [("a",), ("a ",)]
    assert database.staged_rows[-1][1] == [{"payload": "a "}, {"payload": "a "}]
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in database.opened)


def test_gp_effective_keys_keep_equivalent_integer_alias_on_common_stage_path(monkeypatch):
    database = effective_key_target(monkeypatch, "BIGINT", empty=True)
    frame = pd.DataFrame({"payload": ["01", "2"]})
    original = frame.copy(deep=True)

    assert _load(frame, "int8", explicit=True) == 2

    assert database.target.rows == [{"payload": 1}, {"payload": 2}]
    assert database.bound_rows == [(1,), (2,)]
    assert database.staged_rows == [({"payload": "BIGINT"}, [{"payload": 1}, {"payload": 2}])]
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("flag", ["dry_run", "return_sql"])
def test_gp_effective_keys_offline_native_assignment_remains_pending_without_io(monkeypatch, flag):
    database = effective_key_target(monkeypatch, "CHARACTER(2)")
    seed = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": ["a", "b"]})
    original = frame.copy(deep=True)

    plan = sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        write_mode="append",
        key_columns="payload",
        table_schema={"payload": "VARCHAR(2)"},
        **{flag: True},
    )

    assert isinstance(plan, SqlPlan)
    assert plan.options["native_assignment_validation_pending"] is True
    assert database.opened == []
    assert database.commands == []
    assert database.staged_rows == []
    assert database.target.schema == {"payload": "CHARACTER(2)"}
    assert database.target.rows == seed
    pd.testing.assert_frame_equal(frame, original)
