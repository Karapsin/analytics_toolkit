[All AB functions](index.md)

# compute_mde_sql_native

Estimate MDE planning scenarios from a SQL historical user-day table while
computing metric statistics inside SQL.

```python
compute_mde_sql_native(
    db_key,
    sql_table_name,
    *,
    sql_where=None,
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
    outliers_policy="non_zero_truncate",
    pre_exp_days=None,
    sum_agg_metrics=None,
    max_agg_metrics=None,
    print_queries=False,
    retry_cnt=5,
    timeout_increment=5,
    query_label=None,
    concurrency=1,
)
```

## Inputs

- `db_key` - connection key or alias from `.connections`
- `sql_table_name` - historical user-day SQL table
- `sql_where` - optional raw SQL predicate applied to validation and aggregate
  stats queries
- `user_id` - user id column
- `date_column` - date column used for historical windows
- `metric_columns` - mean metric columns to include
- `ratio_metrics` - optional ratio metric specifications
- `start_dt` - required first day of the pseudo-experiment outcome window; pass
  `None` explicitly to use the first available historical date after filtering
- `concurrency` - worker concurrency used for independent SQL stats queries
- MDE, grid, outlier, CUPED, and aggregation options match
  [compute_mde](compute-mde.md)

## Usage

```python
from analytics_toolkit.ab_utils import RatioMetricSpec, compute_mde_sql_native

planning = compute_mde_sql_native(
    "analytics_prod",
    "mart.user_day_metrics",
    sql_where="country = 'US'",
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
    exp_days=[7, 14],
    start_dt="2024-01-15",
    group_sizes=[50_000, 100_000],
)
```

## Notes

- The helper validates table existence, column names, non-null user/date values,
  unique user-date rows, and available date span before computing scenarios.
- SQL queries aggregate each selected outcome and pre-period window to one row
  per user inside SQL, then compute averages, variances, outlier-adjusted stats,
  ratio stats, and CUPED variance in SQL.
- Python receives only compact stats result rows and the final planning output;
  it does not load per-user window dataframes.
- Greenplum uses exact percentile SQL for outlier cutoffs, ClickHouse uses
  `quantileExact`, and Trino uses `approx_percentile` because that is the
  portable Trino aggregate available for percentile cutoffs.
- `sql_where` is inserted as a SQL predicate; callers are responsible for safe
  predicate construction.

[All AB functions](index.md)
