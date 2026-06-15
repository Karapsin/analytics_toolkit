[AB utilities index](index.md)

# MDE Planning

For experiment planning without observed groups, use
[compute_mde](functions/compute-mde.md) on historical user-day data. The helper
chooses one calendar window for each requested experiment length, aggregates the
window to one row per user, estimates metric averages and variances, and then
computes MDE for each requested total experiment size.

```python
from analytics_toolkit.ab_utils import (
    RatioMetricSpec,
    compute_mde,
)

planning = compute_mde(
    historical_user_days,
    user_id="user_id",
    date_column="dt",
    metric_columns=["orders", "gmv"],
    ratio_metrics=[
        RatioMetricSpec(
            name="ctr_user",
            numerator="clicks",
            denominator="views",
            level="user",
        )
    ],
    exp_days=[7, 14, 21],
    group_sizes=[50_000, 100_000, 150_000],
    control_share=0.5,
    exp_length_policy="end",
)
```

The result contains one row per metric, experiment length, and total planned
experiment size. `group_size` is total experiment users; the control and test
sizes are derived from `control_share` when computing `mde_abs` and
`mde_relative`.

Use explicit `exp_days` and `group_sizes` lists for irregular scenarios, or use
`min_days`/`max_days`/`days_step` with
`min_group_size`/`max_group_size`/`group_size_step` for regular grids.
`RatioMetricSpec` can also be passed to
[compute_test_metrics](functions/compute-test-metrics.md); see
[Ratio Metrics](ratio-metrics.md) for the dictionary form and
[Metric Comparison Workflow](metric-comparison.md) for experiment analysis.

[AB utilities index](index.md)
