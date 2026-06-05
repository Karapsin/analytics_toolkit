[SQL functions index](index.md)

# execute_read

Run setup SQL statements, then read the final SQL statement into a dataframe on the same connection.

```python
execute_read(connection_type: 'str', query: 'str', print_queries: 'bool' = False, gp_break_query: 'bool' = False, gp_commit_each_statement: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, return_metadata: 'bool' = False, progress: 'bool' = False) -> 'pd.DataFrame | SqlOperationResult'
```

## Inputs

### General Inputs

- `connection_type`: Connection key or alias from `.connections`; backend dispatch is selected from that entry.
- `query`: SQL text to execute or read.
- `retry_cnt`: Number of operation retries with fresh connections.
- `timeout_increment`: Delay increment used between operation retries.
- `return_metadata`: When `True`, return `SqlOperationResult` instead of the historical bare value.
- `print_queries`: Whether to print SQL text before execution.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.
- `progress`: Whether to show progress bars for supported multi-step or row-loading operations.

### Backend-Specific Inputs

- `gp_break_query`: For Greenplum, whether to split and execute multi-statement SQL statement by statement.
- `gp_commit_each_statement`: For Greenplum split execution, whether to commit after each statement.

## Usage

```python
from analytics_toolkit import sql

result = sql.execute_read(
    "gp",
    """
    analyze sandbox.orders;
    select order_date, count(*) as orders
    from sandbox.orders
    group by order_date
    """,
)
```

## Notes

- Every statement except the last is executed first; the last statement is read into a dataframe.

[SQL functions index](index.md)
