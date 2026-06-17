[AB utilities index](index.md)

# Parallel Metric Workflows

Parallel AB workflows are useful when the same metric comparison should run for
several independent segments. The task mapping names each segment and stores
the dataframe, labels, and per-task comparison options. Use
[compute_test_metrics](functions/compute-test-metrics.md) with a task mapping
when the dataframes are already loaded.

```python
from analytics_toolkit.ab_utils import compute_test_metrics

result = compute_test_metrics(
    {
        "segment_1": {
            "df": segment_1_df,
            "pre_exp_df": segment_1_pre_df,
            "labels": {"segment": "segment1"},
            "test_vs_test": False,
        },
        "segment_2": {
            "df": segment_2_df,
            "labels": {"segment": "segment2"},
            "test_vs_test": False,
        },
    },
    concurrency=2,
)
```

When the task data should be read from SQL first, use
[compute_metrics_from_sql](functions/compute-metrics-from-sql.md).
It loads each task through the supported `from analytics_toolkit import sql`
facade and then runs the metric calculations with the same concurrency caps.

```python
from analytics_toolkit.ab_utils import compute_metrics_from_sql

result = compute_metrics_from_sql(
    {
        "segment_1": {
            "sql": "select * from mart.ab_segment_1",
            "pre_exp_sql": "select * from mart.ab_segment_1_pre",
            "labels": {"segment": "segment1"},
            "test_vs_test": False,
        },
        "segment_2": {
            "sql": "select * from mart.ab_segment_2",
            "labels": {"segment": "segment2"},
            "test_vs_test": False,
            "start_comment": "/* segment_2 metrics */",
        },
    },
    db_key="analytics_prod",
    concurrency=2,
    start_comment="/* ab metrics batch */",
)
```

The SQL-backed workflow passes the top-level `start_comment` to task reads. A
task-level `start_comment` overrides it for that task. Both task-map workflows
use `concurrency` as requested fan-out and accept soft and hard caps like
[sql.async_sql](../sql/functions/async_sql.md), so large batches can be bounded
without rewriting the task map.

When `fail_fast=False`, failed tasks return error text instead of aborting the
whole batch. SQL-backed failures include the task name plus the experiment SQL
and pre-experiment SQL when present.

[AB utilities index](index.md)
