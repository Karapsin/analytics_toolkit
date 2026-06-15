[All AB functions](index.md)

# compute_mde_only

Estimate pre-test MDE from historical one-row-per-user data.

```python
compute_mde_only(
    df,
    *,
    user_id,
    n0,
    n1,
    metric_columns=None,
    ratio_metrics=None,
    mde_alpha=0.05,
    mde_power=0.8,
    outliers_quantile=0.999,
    outliers_policy="truncate",
)
```

## Inputs

- `df` - historical one-row-per-user dataframe
- `user_id` - unique user id column
- `n0` - planned control group size
- `n1` - planned test group size
- `metric_columns` - mean metric columns to include
- `ratio_metrics` - optional ratio metric specifications
- `mde_alpha` - significance level used for MDE calculation
- `mde_power` - statistical power used for MDE calculation
- `outliers_quantile` - upper-tail cutoff quantile
- `outliers_policy` - `"truncate"` or `"drop"`

## Usage

```python
from analytics_toolkit.ab_utils import RatioMetricSpec, compute_mde_only

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

Output example:

```python
planning[["metric_name", "metric_baseline", "mde_abs", "mde_relative"]]
#   metric_name  metric_baseline  mde_abs  mde_relative
# 0      orders             0.42     0.01         0.024
# 1         gmv            18.70     0.42         0.022
# 2    ctr_user             0.08     0.01         0.125
```

## Notes

- `user_id` is required and must identify one row per user.
- Pass planning settings directly through function inputs.
- Output includes baseline, variance, standard error, absolute MDE, relative
  MDE, and outlier diagnostics.

[All AB functions](index.md)
