[SQL functions index](index.md)

# gp_cancel_all_running_queries

Cancel current-user Greenplum backend queries except the caller session.

```python
gp_cancel_all_running_queries(connection_key: 'str' = 'gp', concurrency: 'int' = 1, print_queries: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None) -> 'pd.DataFrame'
```

## Inputs

### General Inputs

- `connection_key`: Connection key or alias from `.connections`, or an Airflow connection ID when Airflow routing is active.
- `retry_cnt`: Number of operation retries with fresh connections.
- `timeout_increment`: Delay increment used between operation retries.
- `print_queries`: Whether to print SQL text before execution.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.
- `concurrency`: Maximum requested task or cancellation concurrency.

### Backend-Specific Inputs

None.

## Usage

```python
from analytics_toolkit import sql

cancelled = sql.gp_cancel_all_running_queries("gp", concurrency=4)
print(cancelled)
```

## Notes

- The caller session is excluded from cancellation.

[SQL functions index](index.md)
