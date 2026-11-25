"""Value-aware GP temporal assignment preserves raw input and strict typed facts."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from enum import Enum

import pytest
from analytics_toolkit.sql.backends.gp.temporal_assignment import (
    contains_timestamp_type,
    pending_timestamp_assignment,
    requires_timestamp_text_assignment,
    timestamp_assignment_value,
    timestamp_text_value_required,
)
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile


class Stamp(Enum):
    TEXT = "2025-01-02 03:04:05.123456"
    NULL = None


def profile(values, native="TIMESTAMP"):
    result = ColumnProfile(native_type=native, source_backend="gp")
    for value in values:
        result.add(value)
    return result


def test_pending_timestamp_text_validates_native_values_without_rewriting_profile():
    native = dt.datetime.fromisoformat("2025-01-02T03:04:05.123456")
    selected = profile(["January 8, 1999", native, None])
    original = deepcopy(selected)
    assert pending_timestamp_assignment(selected, "TIMESTAMP")
    assert selected == original
    assert timestamp_assignment_value(native, "TIMESTAMP(3)") is native
    assert timestamp_assignment_value(Stamp.TEXT, "TIMESTAMP") == Stamp.TEXT.value
    assert timestamp_assignment_value(Stamp.NULL, "TIMESTAMP") is None


def test_pending_timestamp_array_preserves_nulls_shape_and_raw_native_clocks():
    native = dt.datetime.fromisoformat("2025-01-02T03:04:05.123456")
    rows = [[native, "2025-01-03 04:05:06"], [None, native]]
    selected = profile([rows], "TIMESTAMP[]")
    original = deepcopy(selected)
    assert pending_timestamp_assignment(selected, "TIMESTAMP[]")
    bound = timestamp_assignment_value(rows, "TIMESTAMP[]")
    assert bound == rows
    assert bound[0][0] is native
    assert bound[1][1] is native
    assert rows == [[native, "2025-01-03 04:05:06"], [None, native]]
    assert selected == original


@pytest.mark.parametrize(
    "values",
    [
        ["2025-01-01", 1],
        [
            "2025-01-01",
            dt.datetime.fromisoformat("2025-01-01T00:00:00.000000"),
            dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
        ],
    ],
)
def test_pending_timestamp_text_does_not_waive_incompatible_nonstring_families(values):
    with pytest.raises(ValueError, match="authoritative timestamp"):
        pending_timestamp_assignment(profile(values), "TIMESTAMP")


def test_requested_precision_change_is_assigned_by_gp_but_existing_schema_stays_strict():
    native = dt.datetime.fromisoformat("2025-01-02T03:04:05.123500")
    selected = profile([native])
    assert requires_timestamp_text_assignment(selected, "TIMESTAMP(3)")
    assert pending_timestamp_assignment(selected, "TIMESTAMP(3)")
    assert timestamp_assignment_value(native, "TIMESTAMP(3)") is native
    with pytest.raises(ValueError, match="timestamp precision"):
        pending_timestamp_assignment(selected, "TIMESTAMP(3)", allow_timestamp_precision_loss=False)


def test_native_timestamp_fastpath_and_unrelated_types_do_not_become_pending():
    selected = profile([dt.datetime.fromisoformat("2025-01-02T00:00:00.000000"), None])
    assert not pending_timestamp_assignment(selected, "TIMESTAMP")
    assert not contains_timestamp_type("TEXT")
    assert not pending_timestamp_assignment(profile(["a"]), "TEXT")
    assert not timestamp_text_value_required("a", "TEXT")
    assert timestamp_text_value_required([[Stamp.TEXT, Stamp.NULL]], "TIMESTAMP[]")
