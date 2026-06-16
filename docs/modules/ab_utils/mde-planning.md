[AB utilities index](index.md)

# MDE Planning

For experiment planning without observed groups, use
[compute_mde](functions/compute-mde.md) on historical user-day data, or
[compute_mde_from_sql](functions/compute-mde-from-sql.md) when loading the full
historical table into Python is too expensive. The helpers use an
experiment-like outcome window for each requested experiment length, aggregate
the window to one row per user, estimate metric averages and variances, and then
compute MDE for each requested total experiment size. They also estimate CUPED
MDE from an adjacent pre-experiment window when it is available.

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
        ),
        RatioMetricSpec(
            name="conversion_rate",
            numerator="converted",
            denominator="views",
            level="agg",
        )
    ],
    max_agg_metrics=["converted"],
    exp_days=[7, 14, 21],
    start_dt="2024-01-15",
    group_sizes=[50_000, 100_000, 150_000],
    control_share=0.5,
    pre_exp_days=None,
)
```

The result contains one row per metric, experiment length, and total planned
experiment size. `group_size` is total experiment users; the control and test
sizes are derived from `control_share` when computing `mde_abs`,
`mde_relative`, `mde_abs_cuped`, and `mde_relative_cuped`.

By default, `compute_mde` sums metric columns and ratio numerator/denominator
components inside each selected user window. Pass `max_agg_metrics` for period
indicator columns such as conversion flags; ratio metrics apply the selected
aggregation independently to numerator and denominator components before the
ratio statistics are computed. Pass `sum_agg_metrics` instead when most metrics
should use max aggregation and only selected columns should keep sum
aggregation.

The outcome window starts at the required `start_dt` argument. Pass
`start_dt=None` explicitly to start at the first available historical date. By
default, CUPED uses a pre-period with the same length as each `exp_days`
scenario. Pass `pre_exp_days` to use one fixed pre-period length across all
scenarios. If the adjacent pre-period before the outcome window is unavailable,
CUPED columns are `NaN` and a warning is emitted.

For SQL-backed planning, pass the table and connection alias directly:

```python
from analytics_toolkit.ab_utils import compute_mde_from_sql

planning = compute_mde_from_sql(
    "analytics_prod",
    "mart.user_day_metrics",
    sql_where="country = 'US'",
    metric_columns=["orders", "gmv"],
    exp_days=[7, 14],
    start_dt="2024-01-15",
    group_sizes=[50_000, 100_000],
    concurrency=2,
)
```

Use explicit `exp_days` and `group_sizes` lists for irregular scenarios, or use
`min_days`/`max_days`/`days_step` with
`min_group_size`/`max_group_size`/`group_size_step` for regular grids.
For SQL-backed planning, `concurrency > 1` computes day-size combinations in
parallel after metadata and source validation have completed.
`RatioMetricSpec` can also be passed to
[compute_test_metrics](functions/compute-test-metrics.md); see
[Ratio Metrics](ratio-metrics.md) for the dictionary form and
[Metric Comparison Workflow](metric-comparison.md) for experiment analysis.

[AB utilities index](index.md)
