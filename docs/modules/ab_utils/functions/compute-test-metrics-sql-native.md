[All AB functions](index.md)

# compute_test_metrics_sql_native

Compare experiment groups across mean and ratio metrics from SQL-side aggregate
statistics without loading user-grain experiment dataframes into Python.

```python
compute_test_metrics_sql_native(
    db_key,
    source,
    *,
    source_type="table",
    sql_where=None,
    pre_exp_source=None,
    pre_exp_source_type="table",
    pre_exp_sql_where=None,
    group="group_name",
    control="control",
    user_id="user_id",
    metric_columns=None,
    mde_alpha=0.05,
    mde_power=0.8,
    ratio_metrics=None,
    test_vs_test=True,
    multiple_comparisons_adjustment=False,
    multiple_comparisons_adjustment_resamples=2000,
    bootstrap_random_state=0,
    bootstrap_n_jobs=1,
    bootstrap_progress=False,
    bootstrap_large_source_row_threshold=100000,
    bootstrap_large_source_resamples_per_query=10,
    outliers_quantile=0.999,
    outliers_policy="non_zero_truncate",
    concurrency=1,
    fail_fast=True,
    soft_concurrency_cap=None,
    hard_concurrency_cap=5,
    progress=False,
    print_queries=False,
    retry_cnt=5,
    timeout_increment=5,
    query_label=None,
)
```

## Inputs

- `db_key` - connection alias used for SQL reads
- `source` - SQL table name, raw SQL string, or mapping of named SQL-native tasks
- `source_type` - `"table"` or `"sql"` for string sources
- `sql_where` - optional raw SQL predicate applied to the experiment source
- `pre_exp_source` - optional SQL table or raw SQL source for CUPED inputs
- `pre_exp_source_type` - `"table"` or `"sql"` for `pre_exp_source`
- `pre_exp_sql_where` - optional raw SQL predicate applied to the CUPED source
- `group` - column containing experiment group labels
- `control` - label of the control group
- `user_id` - unique user id column
- `metric_columns` - optional mean metric columns; when omitted, numeric non-reserved columns are inferred where metadata is available
- `ratio_metrics` - optional ratio metric specifications
- `test_vs_test` - whether to compare test groups against each other
- `multiple_comparisons_adjustment` - whether to compute centered, studentized
  SQL bootstrap max-T adjustment
- `multiple_comparisons_adjustment_resamples` - total bootstrap replicate count
- `bootstrap_random_state` - non-negative deterministic bootstrap seed, or `None` to choose
  one seed for the whole call
- `bootstrap_n_jobs` - must be `1`; SQL-native bootstrap batches run sequentially
- `bootstrap_progress` - whether to show progress across bootstrap query batches
- `bootstrap_large_source_row_threshold` - validated source row count at which
  the large-source per-query cap applies
- `bootstrap_large_source_resamples_per_query` - maximum resamples in each
  large-source query, subject to the sampled-row budget
- `outliers_quantile` - upper-tail cutoff quantile
- `outliers_policy` - `"non_zero_truncate"`, `"truncate"`, or `"drop"`
- `concurrency` - task fan-out when `source` is a task mapping
- `fail_fast` - whether task-map mode stops after the first failed task
- `print_queries` - whether SQL reads print generated queries
- `retry_cnt` - SQL read retry count
- `timeout_increment` - SQL retry timeout increment
- `query_label` - optional SQL query label

## Usage

```python
from analytics_toolkit.ab_utils import compute_test_metrics_sql_native

result = compute_test_metrics_sql_native(
    "analytics_prod",
    "mart.ab_user_metrics",
    sql_where="experiment_id = 42",
    group="variant",
    control="control",
    user_id="user_id",
    metric_columns=["orders", "gmv"],
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
# 0      orders        mean    test  control    0.041
# 1         gmv        mean    test  control    0.067
# 2         ctr       ratio    test  control    0.018
```

## Notes

- SQL returns compact validation, group-stat, CUPED, and bootstrap summary rows;
  Python does not load user-grain experiment or pre-period dataframes
- Table sources use SQL metadata for backend and column discovery
- Raw SQL sources are wrapped as subqueries and may need explicit
  `metric_columns` when metadata does not expose useful numeric types
- `sql_where` and `pre_exp_sql_where` are inserted as SQL predicates; callers
  are responsible for safe predicate construction
- Bootstrap summaries are deterministic for a fixed `bootstrap_random_state`,
  but they are backend-native and not bit-for-bit identical to NumPy bootstrap
  samples
- Bootstrap rows are resampled within their observed groups, preserving group
  sizes and complete metric-component rows. Every replicate recomputes pooled
  outlier handling and uses `(bootstrap delta - observed delta) / bootstrap
  standard error` for the metric-family max-T distribution.
- `bootstrap_adj_p` uses `(1 + exceedances) / (1 + valid family replicates)`.
  A family replicate is discarded when any observed-valid comparison has a
  non-finite centered statistic; discarded replicates warn, and no valid family
  replicates produces `NaN`.
- Small sources use up to 250 resamples per query while targeting at most about
  five million sampled rows. Sources at or above
  `bootstrap_large_source_row_threshold` additionally use
  `bootstrap_large_source_resamples_per_query` as a cap. Batches are always
  sequential and return only compact counts and moments to Python.
- Multi-query bootstrap assumes the filtered source remains unchanged until all
  batches finish. A warning reports the batch count and largest sampled-row
  estimate when more than one query is required.

[All AB functions](index.md)
