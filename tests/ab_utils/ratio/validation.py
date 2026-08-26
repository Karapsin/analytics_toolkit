from __future__ import annotations

from tests.ab_utils._support.edge_cases import (
    _ratio_source,
    math,
    np,
    pd,
    pytest,
    ratio_module,
)


@pytest.mark.parametrize(
    ("specs", "reserved", "error", "message"),
    [
        (["bad"], set(), TypeError, "must be a dictionary"),
        (
            [{"name": "r", "numerator": "num", "denominator": "den", "level": "row"}],
            set(),
            ValueError,
            "invalid level",
        ),
        (
            [
                {
                    "name": "r",
                    "numerator": "num",
                    "denominator": "den",
                    "invalid_denominator": "raise",
                }
            ],
            set(),
            ValueError,
            "invalid invalid_denominator",
        ),
        (
            [
                {"name": "r", "numerator": "num", "denominator": "den"},
                {"name": "r", "numerator": "num", "denominator": "den"},
            ],
            set(),
            ValueError,
            "Duplicate ratio metric",
        ),
        (
            [{"name": "r", "numerator": "num", "denominator": "den"}],
            {"num"},
            ValueError,
            "reserved column",
        ),
        (
            [{"name": "r", "numerator": "missing", "denominator": "den"}],
            set(),
            ValueError,
            "missing column",
        ),
        (
            [{"name": "r", "numerator": "num"}],
            set(),
            ValueError,
            "missing required key",
        ),
        (
            [{"name": " ", "numerator": "num", "denominator": "den"}],
            set(),
            ValueError,
            "empty 'name'",
        ),
    ],
)
def test_normalize_ratio_metrics_rejects_invalid_specs(
    specs: list[object],
    reserved: set[str],
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        ratio_module._normalize_ratio_metrics(_ratio_source(), specs, reserved)


def test_agg_ratio_linearization_reports_invalid_denominators() -> None:
    values, reason = ratio_module._build_agg_ratio_linearized_values(
        pd.Series([np.nan]),
        pd.Series([np.nan]),
    )
    assert values.isna().all()
    assert reason == "no valid numerator/denominator pairs"

    values, reason = ratio_module._build_agg_ratio_linearized_values(
        pd.Series([1.0, 2.0]),
        pd.Series([-1.0, 0.0]),
    )
    assert values.isna().all()
    assert reason == "aggregate denominator sum is non-positive"


def test_agg_ratio_stats_handle_empty_and_nonpositive_denominators() -> None:
    empty = pd.DataFrame(columns=["numerator", "denominator"])
    assert ratio_module._compute_agg_ratio_group_stats(empty)["n"] == 0
    assert math.isnan(ratio_module._compute_agg_ratio_group_stats(empty)["ratio"])

    nonpositive = pd.DataFrame({"numerator": [1.0, 2.0], "denominator": [-1.0, 0.0]})
    assert math.isnan(ratio_module._compute_agg_ratio_group_stats(nonpositive)["ratio"])
    assert math.isnan(
        ratio_module._compute_agg_ratio_diff_standard_error(
            empty,
            math.nan,
            nonpositive,
            math.nan,
        )
    )


def test_agg_ratio_variance_rejects_invalid_mean_and_variance() -> None:
    nonpositive = pd.DataFrame({"numerator": [1.0, 2.0], "denominator": [-1.0, -1.0]})
    assert math.isnan(ratio_module._compute_agg_ratio_variance(nonpositive, 1.0))

    missing = pd.DataFrame({"numerator": [np.nan, np.nan], "denominator": [1.0, 1.0]})
    assert math.isnan(ratio_module._compute_agg_ratio_variance(missing, 1.0))


def test_agg_ratio_array_stats_handle_empty_and_nonpositive_denominators() -> None:
    empty_stats = ratio_module._compute_agg_ratio_group_stats_arrays(
        np.array([], dtype=float),
        np.array([], dtype=float),
    )
    assert empty_stats["n"] == 0
    assert math.isnan(empty_stats["ratio"])

    invalid_stats = ratio_module._compute_agg_ratio_group_stats_arrays(
        np.array([1.0]),
        np.array([0.0]),
    )
    assert invalid_stats["n"] == 1
    assert math.isnan(invalid_stats["ratio"])
