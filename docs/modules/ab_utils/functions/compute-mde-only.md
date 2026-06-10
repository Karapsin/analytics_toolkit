[All AB functions](index.md)

# compute_mde_only

Estimate pre-test MDE from historical one-row-per-user data.

```python
compute_mde_only(
    df,
    *,
    n0=None,
    n1=None,
    metric_columns=None,
    ratio_metrics=None,
    user_id=None,
    options=None,
    mde_alpha=0.05,
    mde_power=0.8,
    outliers_quantile=0.999,
    outliers_policy="truncate",
)
```

## Inputs

- `df`: Historical one-row-per-user dataframe.
- `n0`: Planned control group size.
- `n1`: Planned test group size.
- `metric_columns`: Mean metric columns to include.
- `ratio_metrics`: Optional ratio metric specifications.
- `options`: Optional `MdePlanningOptions` bundle.
- `user_id`: Optional unique user id column.
- `mde_alpha`: Significance level used for MDE calculation.
- `mde_power`: Statistical power used for MDE calculation.
- `outliers_quantile`: Upper-tail cutoff quantile.
- `outliers_policy`: `"truncate"` or `"drop"`.

## Usage

```python
from analytics_toolkit.ab_utils import RatioMetricSpec, compute_mde_only

planning = compute_mde_only(
    historical_df,
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
planning[["metric", "baseline", "mde_absolute", "mde_relative"]]
#       metric  baseline  mde_absolute  mde_relative
# 0     orders      0.42          0.01          0.024
# 1        gmv     18.70          0.42          0.022
# 2   ctr_user      0.08          0.01          0.125
```

## Notes

- Pass either `n0` and `n1` directly or through `MdePlanningOptions`.
- Output includes baseline, variance, standard error, absolute MDE, relative
  MDE, and outlier diagnostics.

[All AB functions](index.md)
