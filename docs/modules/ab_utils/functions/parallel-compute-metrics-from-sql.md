[All AB functions](index.md)

# parallel_compute_metrics_from_sql

Load named SQL-backed metric tasks, then run metric computation concurrently.

```python
parallel_compute_metrics_from_sql(
    tasks,
    db,
    *,
    concurrency=5,
    fail_fast=True,
    start_comment=None,
    soft_concurrency_cap=None,
    hard_concurrency_cap=10,
    progress=False,
)
```

## Inputs

- `tasks`: Mapping of task names to SQL-backed metric task dictionaries.
- `db`: SQL connection alias used for task reads.
- `concurrency`: Requested SQL loading and metric task fan-out.
- `fail_fast`: Whether to stop after the first failed task.
- `start_comment`: Optional SQL comment prepended to task reads.
- `soft_concurrency_cap`: Optional throttle below requested concurrency.
- `hard_concurrency_cap`: Maximum allowed effective concurrency.
- `progress`: Whether to show progress output.

## Usage

```python
from analytics_toolkit.ab_utils import parallel_compute_metrics_from_sql

result = parallel_compute_metrics_from_sql(
    {
        "segment_a": {
            "sql": "select * from mart.ab_segment_a",
            "labels": {"segment": "a"},
            "test_vs_test": False,
        },
    },
    db="analytics_prod",
    concurrency=2,
)
```

## Notes

- Task-level `start_comment` overrides the top-level value.
- Failures include the metrics task name and related SQL text.

[All AB functions](index.md)
