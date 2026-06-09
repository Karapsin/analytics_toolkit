[SQL functions index](index.md)

# read

Run one SQL query through a configured connection and return a pandas dataframe.

```python
read(db_key: 'str', query: 'str', print_queries: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, return_metadata: 'bool' = False) -> 'pd.DataFrame | SqlOperationResult'
```

## Inputs

- `db_key`: Connection key or alias from `.connections`; backend dispatch is selected from that entry.
- `query`: SQL text to execute or read.
- `retry_cnt`: Number of operation retries with fresh connections.
- `timeout_increment`: Delay increment used between operation retries.
- `return_metadata`: When `True`, return `SqlOperationResult` instead of the historical bare value.
- `print_queries`: Whether to print SQL text before execution.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.

## Usage

```python
from analytics_toolkit import sql

orders = sql.read(
    db_key="gp",
    query="select order_id, user_id, amount from sandbox.orders limit 100",
)
```

## Notes

- Prefer this short entrypoint in user-facing examples.

[SQL functions index](index.md)
