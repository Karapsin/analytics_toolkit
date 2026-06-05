[AB utilities index](index.md)

# Parallel Compute Metrics From SQL

Load each task dataframe from the same SQL connection alias with
`parallel_compute_metrics_from_sql`:

```python
result = parallel_compute_metrics_from_sql(
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
    db="analytics_prod",
    concurrency=2,
    start_comment="/* ab metrics batch */",
)
```

The top-level `start_comment` is passed to the SQL reads created for each task.
A task-level `start_comment` overrides it and applies to both `sql` and
`pre_exp_sql` for that metrics task. `soft_concurrency_cap` and
`hard_concurrency_cap` are applied to both the SQL-loading phase and the metric
calculation phase.
When a SQL-backed task fails during SQL loading or metric computation,
`parallel_compute_metrics_from_sql` prints the metrics task name plus both the
experiment `sql` and the `pre_exp_sql` value, if one was provided.

[AB utilities index](index.md)
