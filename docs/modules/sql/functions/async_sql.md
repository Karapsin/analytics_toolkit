[SQL functions index](index.md)

# async_sql

Run independent SQL task specs concurrently through the existing synchronous helpers.

```python
async_sql(tasks: 'Sequence[Mapping[str, Any]]', *, concurrency: 'int' = 5, fail_fast: 'bool' = True, start_comment: 'str | None' = None, soft_concurrency_cap: 'int | None' = None, hard_concurrency_cap: 'int' = 10, progress: 'bool' = False) -> 'dict[str, Any]'
```

## Inputs

- `tasks`: Sequence of batch task specifications.
- `concurrency`: Maximum requested task or cancellation concurrency.
- `fail_fast`: Whether a batch should raise after the first failed task and cancel pending tasks.
- `start_comment`: Raw SQL prefix applied to supported task queries.
- `soft_concurrency_cap`: Optional lower cap for actual worker execution inside a larger requested batch.
- `hard_concurrency_cap`: Maximum allowed actual worker concurrency after soft throttling.
- `progress`: Whether to show progress bars for supported multi-step or row-loading operations.

## Usage

```python
from analytics_toolkit import sql

tasks = [
    {"name": "orders", "type": "read", "connection_type": "gp", "query": "select * from sandbox.orders limit 10"},
    {"name": "refresh", "type": "execute", "connection_type": "gp", "query": "analyze sandbox.orders"},
]

result = sql.async_sql(tasks, concurrency=2)
orders = result["orders"]
```

## Notes

- The function itself is synchronous from the caller perspective; it returns a result dictionary.

[SQL functions index](index.md)
