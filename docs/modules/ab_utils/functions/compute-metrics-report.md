[All AB functions](index.md)

# compute_metrics_report

Compute SQL-native AB metrics for the complete source, optionally repeat the
calculations for each value of a segment column, return one long-form dataframe,
and optionally write a formatted Excel workbook.

```python
compute_metrics_report(
    table_name,
    segment=None,
    *,
    db_key,
    sql_where=None,
    pre_exp_table_name=None,
    pre_exp_sql_where=None,
    group="group_name",
    control="control",
    user_id="user_id",
    metric_columns=None,
    mde_alpha=0.05,
    mde_power=0.8,
    ratio_metrics=None,
    test_vs_test=False,
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
    pooled_test_group="test_all",
    all_segment_label="ALL",
    metric_names_override=None,
    groups_order=None,
    create_excel=True,
    excel_file_name=None,
    report_significance_alpha=0.01,
)
```

## Report inputs

- `table_name` - one-row-per-user SQL table
- `segment` - optional column used to add per-segment calculations; omit it to
  compute only the total comparisons
- `db_key` - configured SQL connection alias
- `pre_exp_table_name` - optional compatible pre-experiment table for CUPED
- `metric_columns` - mean metrics; numeric non-ID/group/segment columns are inferred when omitted
- `ratio_metrics` - ratio metric specifications accepted by the other AB metric helpers
- `pooled_test_group` - name assigned to the additional pooled non-control group
- `metric_names_override` - mapping from calculated metric names to final display names
- `groups_order` - preferred group order; observed groups omitted from the list are appended
- `create_excel` - whether to write the workbook
- `excel_file_name` - output filename or path; defaults to `<table>_metrics.xlsx` in the current directory
- `report_significance_alpha` - p-value threshold used to display significant uplifts

The statistical, bootstrap, outlier, concurrency, retry, and progress arguments
have the same behavior as
[compute_test_metrics_sql_native](compute-test-metrics-sql-native.md). The
report defaults `test_vs_test` to `False` so its summary focuses on comparisons
against the control group.

## Usage

```python
from analytics_toolkit.ab_utils import compute_metrics_report

metrics = compute_metrics_report(
    "mart.experiment_user_metrics",
    "customer_segment",
    db_key="analytics_prod",
    pre_exp_table_name="mart.experiment_user_metrics_pre",
    metric_columns=["orders", "revenue"],
    ratio_metrics=[
        {"name": "aov", "numerator": "revenue", "denominator": "orders"},
    ],
    metric_names_override={"orders": "Orders per user", "aov": "AOV"},
    groups_order=["control", "test_3", "test_2", "test_1", "test_all"],
    excel_file_name="experiment_metrics.xlsx",
)
```

When `segment` is provided, the returned dataframe starts with that column and
contains `ALL` before the distinct non-null segment values. When it is omitted,
the dataframe contains only total comparisons and has no synthetic segment
column. In both modes, the workbook contains `summary` and `raw_metrics` sheets.
Set `create_excel=False` to return the same dataframe without creating or
changing a file.

[All AB functions](index.md)
