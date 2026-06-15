[All AB functions](index.md)

# compute_test_metrics

Compare experiment groups across mean and ratio metrics in one
one-row-per-user dataframe.

```python
compute_test_metrics(
    df,
    group="group_name",
    control="control",
    user_id="user_id",
    mde_alpha=0.05,
    mde_power=0.8,
    ratio_metrics=None,
    test_vs_test=True,
    multiple_comparisons_adjustment=False,
    multiple_comparisons_adjustment_resamples=2000,
    bootstrap_random_state=0,
    bootstrap_n_jobs=1,
    bootstrap_progress=False,
    pre_exp_metrics_df=None,
    outliers_quantile=0.999,
    outliers_policy="truncate",
)
```

## Inputs

- `df` - experiment dataframe with one row per user
- `group` - column containing experiment group labels
- `control` - label of the control group
- `user_id` - unique user id column
- `ratio_metrics` - optional ratio metric specifications
- `test_vs_test` - whether to compare test groups against each other
- `pre_exp_metrics_df` - optional pre-experiment dataframe for CUPED outputs
- `mde_alpha` - significance level used for MDE calculation
- `mde_power` - statistical power used for MDE calculation
- `multiple_comparisons_adjustment` - whether to add bootstrap-adjusted outputs
- `multiple_comparisons_adjustment_resamples` - bootstrap resample count
- `bootstrap_random_state` - bootstrap random seed, or `None`
- `bootstrap_n_jobs` - number of bootstrap workers
- `bootstrap_progress` - whether to show bootstrap progress
- `outliers_quantile` - upper-tail cutoff quantile, where `1` leaves the maximum value unmodified
- `outliers_policy` - `"truncate"` or `"drop"`

## Usage

```python
from analytics_toolkit.ab_utils import compute_test_metrics

result = compute_test_metrics(
    experiment_df,
    group="group_name",
    control="control",
    user_id="user_id",
    ratio_metrics=[
        {"name": "ctr", "numerator": "clicks", "denominator": "views"},
    ],
    test_vs_test=False,
)
```

Output example:

```python
result[["metric", "metric_type", "group A", "group B", "p-value"]]
#   metric metric_type  group A  group B  p-value
# 0 orders        mean  control   test_1    0.041
# 1    ctr       ratio  control   test_1    0.018
```

## Notes

- All numeric columns not used as `group`, `user_id`, or ratio components are
  treated as mean metrics.
- Missing metric values are ignored per metric and group.
- CUPED failures warn and return `NaN` for CUPED outputs after validation has
  passed.

[All AB functions](index.md)
