[AB utilities index](index.md)

# MDE Planning

For experiment planning without observed groups, use
[compute_mde_only](functions/compute-mde-only.md) on a historical
one-row-per-user dataframe:

```python
from analytics_toolkit.ab_utils import (
    RatioMetricSpec,
    compute_mde_only,
)

planning = compute_mde_only(
    historical_df,
    user_id="user_id",
    n0=50_000,
    n1=50_000,
    metric_columns=["orders", "gmv"],
    ratio_metrics=[
        RatioMetricSpec(
            name="ctr_user",
            numerator="clicks",
            denominator="views",
            level="user",
        )
    ],
)
```

The result contains the historical sample size, planned group sizes, baseline,
variance, standard error, absolute MDE, relative MDE, and outlier diagnostics for
each mean or ratio metric. `RatioMetricSpec` can also be passed to
[compute_test_metrics](functions/compute-test-metrics.md); see
[Ratio Metrics](ratio-metrics.md) for the dictionary form and
[Metric Comparison Workflow](metric-comparison.md) for experiment analysis.

[AB utilities index](index.md)
