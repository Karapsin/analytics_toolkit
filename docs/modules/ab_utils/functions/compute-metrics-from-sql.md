[All AB functions](index.md)

# compute_metrics_from_sql

Load named SQL-backed metric tasks, then run metric computation concurrently.

```python
compute_metrics_from_sql(
    tasks,
    db_key,
    *,
    concurrency=1,
    fail_fast=True,
    start_comment=None,
    soft_concurrency_cap=None,
    hard_concurrency_cap=10,
    progress=False,
    **metric_defaults,
)
```

## Inputs

- `tasks` - mapping of task names to SQL-backed metric task dictionaries
- `db_key` - connection alias used for SQL task reads
- `concurrency` - requested SQL loading and metric task fan-out
- `fail_fast` - whether to stop after the first failed task
- `start_comment` - optional SQL comment prepended to task reads
- `soft_concurrency_cap` - optional throttle below requested concurrency
- `hard_concurrency_cap` - maximum allowed effective concurrency
- `progress` - whether to show progress output
- `metric_defaults` - any non-dataframe `compute_test_metrics` inputs to apply to
  every task, such as `group`, `test_vs_test`, `ratio_metrics`,
  `bootstrap_progress`, `outliers_quantile`, or `outliers_policy`

## Usage

```python
from analytics_toolkit.ab_utils import compute_metrics_from_sql

result = compute_metrics_from_sql(
    {
        "segment_a": {
            "sql": "select * from mart.ab_segment_a",
            "labels": {"segment": "a"},
            "test_vs_test": False,
        },
    },
    db_key="analytics_prod",
    concurrency=2,
    outliers_quantile=0.999,
    test_vs_test=False,
)
```

Output example:

```python
result.keys()
# dict_keys(['segment_a'])

result["segment_a"][["metric", "group A", "group B", "p-value"]].head()
#   metric  group A group B  p-value
# 0 orders  control  test_1    0.041
```

## Notes

- Task-level `start_comment` overrides the top-level value.
- Task-level metric inputs override `metric_defaults`.
- Failures include the metrics task name and related SQL text.

[All AB functions](index.md)
