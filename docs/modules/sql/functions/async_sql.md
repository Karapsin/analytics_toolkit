[SQL functions index](index.md)

# async_sql

Run independent SQL task specs concurrently through the existing synchronous helpers.

```python
async_sql(tasks: 'Sequence[Mapping[str, Any]]', *, concurrency: 'int' = 5, fail_fast: 'bool' = True, start_comment: 'str | None' = None, soft_concurrency_cap: 'int | None' = None, hard_concurrency_cap: 'int' = 5, progress: 'bool' = False) -> 'dict[str, Any]'
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
    {"name": "refresh", "type": "execute", "db_key": "gp", "query": "analyze sandbox.orders"},
]

result = sql.async_sql(tasks, concurrency=2)
orders = result["orders"]
```

Output example:

```python
result.keys()
# dict_keys(['orders', 'refresh'])

orders.head()
#    order_id  user_id  amount
# 0      1001       42   19.90
```

## Notes

- The function itself is synchronous from the caller perspective; it returns a result dictionary.
- Interrupting the caller with Ctrl+C stops queued task dispatch and attempts to
  cancel in-flight toolkit SQL started by that batch on Greenplum, Trino, and
  ClickHouse. Cleanup is limited to 10 seconds; unresolved cancellation is
  logged before the original `KeyboardInterrupt` is re-raised.
- Cancellation uses a private per-batch SQL marker, so other queries owned by
  the same database user are not targeted. Toolkit SQL called from custom
  pipeline steps participates automatically; raw driver calls made directly by
  user code do not.
- Task names must be unique. This includes collisions with generated names such
  as `task_1` for an unnamed task at index `1`; duplicates are rejected before
  any task starts.

[SQL functions index](index.md)
