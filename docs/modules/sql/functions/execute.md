[SQL functions index](index.md)

# execute

Run one SQL string or a list of independent SQL queries through a configured connection without returning a dataframe.

```python
execute(db_key: 'str', query: 'str | list[str]', print_queries: 'bool' = False, gp_break_query: 'bool' = False, gp_commit_each_statement: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, progress: 'bool' = False, concurrency: 'int' = 1, soft_concurrency_cap: 'int | None' = None, hard_concurrency_cap: 'int' = 5) -> 'Any'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias from `.connections`; backend dispatch is selected from that entry
- `query` - one SQL string, or a non-empty list of independent SQL strings
- `retry_cnt` - number of operation retries with fresh connections
- `timeout_increment` - delay increment used between operation retries
- `dry_run` - when `True`, return a plan without mutating the database
- `return_sql` - when `True`, return a `SqlPlan` instead of mutating a database
- `return_metadata` - when `True`, return `SqlOperationResult` instead of the historical bare value
- `print_queries` - whether to print SQL text before execution
- `query_label` - safe label added to generated SQL comments, plans, metadata, and logs
- `progress` - whether to show progress bars for supported multi-step or row-loading operations
- `concurrency` - maximum requested workers for list input; defaults to sequential execution with `1` and has no effect on string input
- `soft_concurrency_cap` - optional lower ceiling applied to list workers without rejecting a higher requested `concurrency`
- `hard_concurrency_cap` - safety ceiling for effective list concurrency; defaults to `5` and rejects requests above the ceiling

### Backend-Specific Inputs

- `gp_break_query` - for Greenplum, whether to split and execute multi-statement SQL statement by statement
- `gp_commit_each_statement` - for Greenplum split execution, whether to commit after each statement

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

Execute independent queries concurrently:

```python
results = sql.execute(
    db_key="gp",
    query=[
        "analyze sandbox.orders",
        "analyze sandbox.customers",
        "analyze sandbox.products",
    ],
    concurrency=3,
    soft_concurrency_cap=2,
)

# results == [None, None, None], in input order.
```

## Notes

- Prefer this short entrypoint in user-facing examples.
- List entries use independent connections and retry lifecycles. Retriable errors are retried through `retry_cnt`; after retries are exhausted, queued work is cancelled and the original error is raised. Already-running or completed queries are not rolled back as a batch.
- List results preserve input order. `dry_run=True` or `return_sql=True` returns one `SqlPlan` per query, and `return_metadata=True` returns one `SqlOperationResult` per query.
- For Trino and ClickHouse, multi-statement SQL is split and submitted sequentially.
- For Greenplum, pass `gp_break_query=True` when statement-by-statement execution is needed.

[SQL functions index](index.md)
