"""NOT NULL preflight uses certified top-level SQL NULLs, not container truth."""

from enum import Enum

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.dml.load.type_validation import (
    _validated_dataframe_profiles,
    convert_dataframe_schema,
    validate_dataframe_schema,
)


class _Missing(Enum):
    VALUE = None


@pytest.mark.parametrize("value", [None, np.nan, np.float32("nan"), pd.NA, pd.NaT, _Missing.VALUE])
@pytest.mark.parametrize("native", ["BIGINT", "TEXT", "NUMERIC(4,0)"])
def test_converted_null_profiles_count_logical_missing_values(value, native):
    frame = pd.DataFrame({"value": pd.Series([value], dtype=object)})
    profiles = _validated_dataframe_profiles(frame, {"value": native}, "gp")
    assert profiles["value"].null_count == profiles["value"].count == 1
    assert validate_dataframe_schema(frame, {"value": native}, "gp") is None


@pytest.mark.parametrize("value", ["NULL", "None", "NaN", "", [None], {"key": None}])
def test_text_output_containing_null_spellings_or_members_is_not_sql_null(value):
    frame = pd.DataFrame({"value": pd.Series([value], dtype=object)})
    profiles = _validated_dataframe_profiles(frame, {"value": "TEXT"}, "gp")
    assert profiles["value"].count == 1
    assert profiles["value"].null_count == 0
    assert profiles["value"].families == {"string"}


@pytest.mark.parametrize(("backend", "native"), [("gp", "BIGINT[]"), ("trino", "ARRAY(BIGINT)")])
def test_native_array_null_members_do_not_make_the_column_null(backend, native):
    value = [None, pd.NA, _Missing.VALUE]
    frame = pd.DataFrame({"value": pd.Series([value], dtype=object)})
    profiles = _validated_dataframe_profiles(frame, {"value": native}, backend)
    profile = profiles["value"]
    assert profile.null_count == 0
    assert profile.count == 1
    assert profile.children[0].null_count == 3
    assert frame.iloc[0, 0] is value


def test_target_null_summary_follows_explicit_stage_conversion():
    original = pd.DataFrame({"value": pd.Series([[None], "NULL", _Missing.VALUE], dtype=object)})
    stage, _metadata = convert_dataframe_schema(original, {"value": "TEXT"}, "gp")
    profiles = _validated_dataframe_profiles(
        stage, {"value": "VARCHAR(200)"}, "gp", allow_timestamp_precision_loss=False
    )
    assert stage.iloc[0, 0].startswith('{"v":1,')
    assert stage.iloc[1, 0] == "NULL"
    assert stage.iloc[2, 0] is None
    assert profiles["value"].count == 3
    assert profiles["value"].null_count == 1


def test_null_profiles_preserve_exact_column_identity_and_complete_input():
    frame = pd.DataFrame(
        {
            "Events": pd.Series(["a", "b", _Missing.VALUE], dtype=object),
            "events": pd.Series(["NULL", "NaN", "None"], dtype=object),
            " with space ": pd.Series([[None], [None], [None]], dtype=object),
        }
    )
    profiles = _validated_dataframe_profiles(frame, dict.fromkeys(frame.columns, "TEXT"), "gp")
    assert list(profiles) == ["Events", "events", " with space "]
    assert {name: profile.null_count for name, profile in profiles.items()} == {
        "Events": 1,
        "events": 0,
        " with space ": 0,
    }
    assert all(profile.count == 3 for profile in profiles.values())


def test_zero_row_profile_has_no_provided_null_values():
    frame = pd.DataFrame({"value": pd.Series([], dtype=object)})
    profile = _validated_dataframe_profiles(frame, {"value": "BIGINT"}, "gp")["value"]
    assert profile.count == profile.null_count == 0
