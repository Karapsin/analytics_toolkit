[SQL functions index](index.md)

# execute

Run one or more SQL statements through a configured connection without returning a dataframe.

```python
execute(db_key: 'str', query: 'str', print_queries: 'bool' = False, gp_break_query: 'bool' = False, gp_commit_each_statement: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, progress: 'bool' = False) -> 'Any'
```

## Inputs

### General Inputs

- `db_key`: Connection key or alias from `.connections`; backend dispatch is selected from that entry.
- `query`: SQL text to execute or read.
- `retry_cnt`: Number of operation retries with fresh connections.
- `timeout_increment`: Delay increment used between operation retries.
- `dry_run`: When `True`, return a plan without mutating the database.
- `return_sql`: When `True`, return a `SqlPlan` instead of mutating a database.
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

sql.execute(
    db_key="gp",
    query="insert into sandbox.order_summary select order_date, count(*) from sandbox.orders group by order_date",
)
```

Output example:

```python
# Returns None after the statement completes.
```

## Notes

- Prefer this short entrypoint in user-facing examples.
- For Trino and ClickHouse, multi-statement SQL is split and submitted sequentially.
- For Greenplum, pass `gp_break_query=True` when statement-by-statement execution is needed.

[SQL functions index](index.md)
