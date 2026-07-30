[All AB functions](index.md)

# compute_test_metrics

Compare experiment groups across mean and ratio metrics in one
one-row-per-user dataframe, or run named dataframe-backed metric tasks.

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
    outliers_policy="non_zero_truncate",
    concurrency=1,
    fail_fast=True,
    soft_concurrency_cap=None,
    hard_concurrency_cap=5,
    progress=False,
)
```

## Inputs

- `df` - experiment dataframe with one row per user, or a mapping of named task dictionaries
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
- `bootstrap_random_state` - non-negative bootstrap random seed, or `None`
- `bootstrap_n_jobs` - number of bootstrap workers
- `bootstrap_progress` - whether to show bootstrap progress
- `outliers_quantile` - upper-tail cutoff quantile, where `1` leaves the maximum value unmodified
- `outliers_policy` - `"non_zero_truncate"`, `"truncate"`, or `"drop"`
- `concurrency` - task fan-out when `df` is a task mapping; must stay `1` for a single dataframe
- `fail_fast` - whether task-map mode stops after the first failed task
- `soft_concurrency_cap` - optional task-map throttle below requested concurrency
- `hard_concurrency_cap` - maximum allowed effective task-map concurrency
- `progress` - whether to show task-map progress output

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
result[["metric_name", "metric_type", "group_1", "group_2", "p-value"]]
#   metric_name metric_type group_1  group_2  p-value
# 0      orders        mean  test_1  control    0.041
# 1         ctr       ratio  test_1  control    0.018
```

Task-map usage:

```python
from analytics_toolkit.ab_utils import compute_test_metrics

result = compute_test_metrics(
    {
        "segment_a": {
            "df": segment_a_df,
            "labels": {"segment": "a"},
            "test_vs_test": False,
        },
        "segment_b": {
            "df": segment_b_df,
            "labels": {"segment": "b"},
            "test_vs_test": False,
        },
    },
    concurrency=2,
)
```

Task-map output example:

```python
result.keys()
# dict_keys(['segment_a', 'segment_b'])

result["segment_a"][["segment", "metric_name", "group_1", "group_2", "p-value"]].head()
#   segment metric_name  group_1  group_2  p-value
# 0       a      orders   test_1  control    0.041
```

## Bootstrap adjustment

When multiple-comparison adjustment is enabled, bootstrap resampling is
stratified by the observed experiment group and preserves complete user rows.
The pooled outlier cutoff and metric estimator are recomputed in every
replicate. `bootstrap_adj_p` uses a centered, studentized max-T distribution
across all observed-valid comparisons for the same metric, with the
finite-sample correction `(1 + exceedances) / (1 + valid replicates)`.

Replicates with a non-finite centered statistic for any observed-valid
comparison are discarded from that metric's max-T family and produce a warning.
If none remain, `bootstrap_adj_p` is `NaN`. `s.e. bootstrap` remains the sample
standard deviation of each row's finite bootstrap deltas. Fixed seeds are
deterministic across worker counts and process-to-thread fallback.

## Notes

- All numeric columns not used as `group`, `user_id`, or ratio components are
  treated as mean metrics.
- Missing metric values are ignored per metric and group.
- The default `"non_zero_truncate"` policy computes the cutoff from non-zero
  metric values, then caps values above that cutoff while keeping zeros in the
  metric sample.
- CUPED failures warn and return `NaN` for CUPED outputs after validation has
  passed.
- `concurrency > 1` is valid only when `df` is a task mapping.
- Task-level `pre_exp_df` is accepted as an alias for `pre_exp_metrics_df`.
- Failed tasks return error text when `fail_fast=False`.

[All AB functions](index.md)
