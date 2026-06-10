[All AB functions](index.md)

# parallel_compute_metrics

Run multiple named `compute_test_metrics` tasks concurrently.

```python
parallel_compute_metrics(
    tasks,
    *,
    concurrency=5,
    fail_fast=True,
    soft_concurrency_cap=None,
    hard_concurrency_cap=10,
    progress=False,
)
```

## Inputs

- `tasks` - mapping of task names to metric task dictionaries
- `concurrency` - requested task fan-out
- `fail_fast` - whether to stop after the first failed task
- `soft_concurrency_cap` - optional throttle below requested concurrency
- `hard_concurrency_cap` - maximum allowed effective concurrency
- `progress` - whether to show progress output

## Usage

```python
from analytics_toolkit.ab_utils import parallel_compute_metrics

result = parallel_compute_metrics(
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

Output example:

```python
result.keys()
# dict_keys(['segment_a', 'segment_b'])

result["segment_a"][["metric", "group A", "group B", "p-value"]].head()
#   metric  group A group B  p-value
# 0 orders  control  test_1    0.041
```

## Notes

- Successful tasks return dataframes.
- Failed tasks return error text when `fail_fast=False`.

[All AB functions](index.md)
