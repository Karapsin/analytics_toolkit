[SQL functions index](index.md)

# parallel_sql

Run independent SQL task specs with thread-based parallelism.

```python
parallel_sql(tasks: 'Sequence[Mapping[str, Any]]', *, concurrency: 'int' = 5, fail_fast: 'bool' = True, start_comment: 'str | None' = None, soft_concurrency_cap: 'int | None' = None, hard_concurrency_cap: 'int' = 10, progress: 'bool' = False) -> 'dict[str, Any]'
```

## Inputs

- `tasks` - sequence of batch task specifications
- `progress` - whether to show progress bars for supported multi-step or row-loading operations
- `fail_fast` - whether a batch should raise after the first failed task and cancel pending tasks
- `start_comment` - raw SQL prefix applied to supported task queries
- `concurrency` - maximum requested task or cancellation concurrency
- `soft_concurrency_cap` - optional lower cap for actual worker execution inside a larger requested batch
- `hard_concurrency_cap` - maximum allowed actual worker concurrency after soft throttling

## Usage

```python
from analytics_toolkit import sql

tasks = [
    {"name": "orders", "type": "read", "db_key": "gp", "query": "select * from sandbox.orders limit 10"},
    {"name": "events", "type": "read", "db_key": "trino", "query": "select * from sandbox.events limit 10"},
]

result = sql.parallel_sql(tasks, concurrency=2)
```

Output example:

```python
result.keys()
# dict_keys(['orders', 'events'])

result["orders"].head()
#    order_id  user_id  amount
# 0      1001       42   19.90
```

## Notes

- Use the same task specification shape as `async_sql`.
- Task names must be unique. This includes collisions with generated names such
  as `task_1` for an unnamed task at index `1`; duplicates are rejected before
  any task starts.

[SQL functions index](index.md)
