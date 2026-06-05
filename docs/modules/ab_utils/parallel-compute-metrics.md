[AB utilities index](index.md)

# Parallel Compute Metrics

Run independent metric jobs in parallel with `parallel_compute_metrics`:

```python
result = parallel_compute_metrics(
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

`parallel_compute_metrics` uses `concurrency` as the requested task fan-out. To
keep accidental fan-out bounded, it also accepts the same cap parameters as
`sql.async_sql` from the supported `from analytics_toolkit import sql` facade:
`soft_concurrency_cap` throttles active metric
workers below the requested concurrency, while `hard_concurrency_cap` rejects an
unthrottled effective concurrency above the cap. The default hard cap is `10`;
set a lower `soft_concurrency_cap` or a higher `hard_concurrency_cap` for larger
batches.

[AB utilities index](index.md)
