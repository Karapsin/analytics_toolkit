"""Shared inputs retain missing values and fresh state across statistical tests."""

from __future__ import annotations

import pandas as pd
import pytest

from tests.ab_utils._support.factories import metric_frame, sample_metrics_df


def test_metric_factory_preserves_explicit_nullable_values_and_group_order() -> None:
    values = pd.array([1, None, 3], dtype="Int64")
    frame = metric_frame(["test", "control", "test"], orders=values)
    values[0] = 99
    assert frame["user_id"].tolist() == [1, 2, 3]
    assert frame["group_name"].tolist() == ["test", "control", "test"]
    assert str(frame["orders"].dtype) == "Int64"
    assert frame.loc[0, "orders"] == 1
    assert frame.loc[1, "orders"] is pd.NA


def test_statistical_sample_factory_does_not_leak_mutations() -> None:
    first = sample_metrics_df()
    expected = sample_metrics_df()
    first.loc[0, "orders"] = 999
    first["extra"] = True
    pd.testing.assert_frame_equal(sample_metrics_df(), expected)
    with pytest.raises(AssertionError):
        pd.testing.assert_frame_equal(first, expected)
