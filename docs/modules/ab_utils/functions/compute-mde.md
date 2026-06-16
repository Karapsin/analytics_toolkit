[All AB functions](index.md)

# compute_mde

Estimate MDE planning scenarios from historical user-day data.

```python
compute_mde(
    df,
    *,
    user_id="user_id",
    metric_columns=None,
    ratio_metrics=None,
    control_share=0.5,
    group_sizes=None,
    min_group_size=None,
    max_group_size=None,
    group_size_step=None,
    date_column="dt",
    exp_days=None,
    min_days=None,
    max_days=None,
    days_step=None,
    start_dt,
    mde_alpha=0.05,
    mde_power=0.8,
    outliers_quantile=0.999,
    outliers_policy="truncate",
    pre_exp_days=None,
    sum_agg_metrics=None,
    max_agg_metrics=None,
)
```

## Inputs

- `df` - historical user-day dataframe with unique user/date rows
- `user_id` - user id column
- `metric_columns` - mean metric columns to include
- `ratio_metrics` - optional ratio metric specifications
- `control_share` - share of total planned experiment users assigned to control
- `group_sizes` - explicit total planned experiment user counts
- `min_group_size` - minimum total planned experiment user count for range mode
- `max_group_size` - maximum total planned experiment user count for range mode
- `group_size_step` - total planned experiment user count step for range mode
- `date_column` - date column used for historical windows
- `exp_days` - explicit experiment lengths in days
- `min_days` - minimum experiment length for range mode
- `max_days` - maximum experiment length for range mode
- `days_step` - experiment length step for range mode
- `start_dt` - required first day of the pseudo-experiment outcome window; pass
  `None` explicitly to use the first available historical date
- `mde_alpha` - significance level used for MDE calculation
- `mde_power` - statistical power used for MDE calculation
- `outliers_quantile` - upper-tail cutoff quantile, where `1` leaves the maximum value unmodified
- `outliers_policy` - `"truncate"` or `"drop"`
- `pre_exp_days` - pre-experiment covariate window length for CUPED MDE;
  `None` uses each `exp_days` value
- `sum_agg_metrics` - metric/component columns to sum inside each user window;
  all other metric/component columns use max aggregation
- `max_agg_metrics` - metric/component columns to max inside each user window;
  all other metric/component columns use sum aggregation

## Usage

```python
from analytics_toolkit.ab_utils import RatioMetricSpec, compute_mde

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
)
```

Output example:

```python
planning[
    [
        "metric_name",
        "avg",
        "var",
        "days",
        "pre_exp_days",
        "group_size",
        "mde_abs",
        "mde_abs_cuped",
    ]
].head()
```

## Notes

- `group_size` is the total planned experiment user count
- control and test sizes are derived from `floor(group_size * control_share)`
- by default, metric columns are summed to one row per user inside each selected outcome window
- pass `max_agg_metrics` for period indicators such as conversion flags; columns not listed there still use sum aggregation
- pass `sum_agg_metrics` to invert the default for a call; listed columns use sum and all other metric/component columns use max
- `sum_agg_metrics` and `max_agg_metrics` cannot be provided together
- ratio metrics aggregate numerator and denominator columns independently before computing statistics
- the pseudo-experiment outcome window starts at `start_dt`; when `start_dt` is
  `None`, it starts at the first available historical date
- CUPED MDE uses the adjacent pre-period immediately before the outcome window
- `mde_relative_cuped` is `mde_abs_cuped / avg`
- if a CUPED estimate cannot be built, CUPED columns are `NaN` and a warning is emitted

[All AB functions](index.md)
