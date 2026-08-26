from __future__ import annotations

from tests.ab_utils._support.edge_cases import (
    math,
    np,
    pd,
    pytest,
    stats_module,
)


@pytest.mark.parametrize(
    ("delta", "standard_error"),
    [
        (math.inf, 1.0),
        (-math.inf, 1.0),
        (1.0, math.inf),
        (1.0, -math.inf),
    ],
)
def test_studentized_statistic_rejects_non_finite_inputs(
    delta: float,
    standard_error: float,
) -> None:
    assert math.isnan(stats_module._compute_studentized_statistic(delta, standard_error))


def test_stats_helpers_cover_empty_and_degenerate_samples() -> None:
    assert math.isnan(stats_module._safe_mean(pd.Series(dtype=float)))
    assert math.isnan(stats_module._compute_sample_variance(pd.Series([1.0])))
    assert math.isnan(stats_module._compute_sample_variance(pd.Series([math.nan, math.nan])))
    assert math.isnan(stats_module._compute_group_diff_standard_error(1.0, 0, 1.0, 2))
    assert math.isnan(stats_module._compute_group_diff_standard_error(math.nan, 2, 1.0, 2))
    assert all(
        math.isnan(value)
        for value in stats_module._compute_ttest_stat_and_p_value(
            pd.Series([1.0]), pd.Series([2.0])
        )
    )
    assert math.isnan(
        stats_module._compute_mde_abs(pd.Series([1.0, 1.0]), pd.Series([math.nan, math.nan]))
    )
    statistic, p_value = stats_module._compute_ttest_stat_and_p_value_arrays(
        np.array([1.0, 2.0]), np.array([2.0, 3.0])
    )
    assert math.isfinite(statistic)
    assert 0 <= p_value <= 1
