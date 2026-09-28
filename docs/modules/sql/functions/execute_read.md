[SQL functions index](index.md)

# execute_read

Run setup SQL statements, then read the final SQL statement into a dataframe on the same connection.

```python
execute_read(db_key: 'str', query: 'str | list[str]', print_queries: 'bool' = False, gp_break_query: 'bool' = False, gp_commit_each_statement: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, return_metadata: 'bool' = False, progress: 'bool' = False, concurrency: 'int' = 1, soft_concurrency_cap: 'int | None' = None, hard_concurrency_cap: 'int' = 5) -> 'pd.DataFrame | SqlOperationResult | list[pd.DataFrame | SqlOperationResult]'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias from `.connections`; backend dispatch is selected from that entry
- `query` - text of SQL to execute or read; a non-empty list runs independent query strings and returns a list in input order
- `concurrency` - requested workers for list input; defaults to `1` and has no effect on string input
- `soft_concurrency_cap` - optional lower ceiling on requested list workers
- `hard_concurrency_cap` - safety ceiling for effective list workers; defaults to `5` and rejects higher effective concurrency
- `retry_cnt` - number of operation retries with fresh connections
- `timeout_increment` - delay increment used between operation retries
- `return_metadata` - when `True`, return `SqlOperationResult` instead of the historical bare value
- `print_queries` - whether to print SQL text before execution
- `query_label` - safe label added to generated SQL comments, plans, metadata, and logs
- `progress` - whether to show progress bars for supported multi-step or row-loading operations

### Backend-Specific Inputs

- `gp_break_query` - for Greenplum, whether to split and execute multi-statement SQL statement by statement
- `gp_commit_each_statement` - for Greenplum split execution, whether to commit after each statement

## Usage

```python
from analytics_toolkit import sql

result = sql.execute_read(
    db_key="gp",
    query="""
    analyze sandbox.orders;
    select order_date, count(*) as orders
    from sandbox.orders
    group by order_date
    """,
)
```

Output example:

```python
result.head()
#    order_date  orders
# 0  2026-06-01    1204
# 1  2026-06-02    1187
```

## Notes

- Every statement except the last is executed first; the last statement is read into a dataframe.
- The returned dataframe uses the same value-inferred pandas nullable extension
  dtypes as `sql.read`, preserving nullable integers without `float64`
  conversion or precision loss.
- Timing logs label setup execution as `[setup]` and the final dataframe query as `[read]`.

## Independent Query Batches

Each list item uses its own connection. Setup statements and the final operation
within that item stay sequential. Items must be independent; temporary tables
and session state are not shared between items. Options apply to every item.
All items are validated before any execution starts. A failed batch raises
`SqlBatchExecutionError` with successful, failed, ambiguous, and cancelled item
outcomes; completed writes are not rolled back across the batch.

```python
from analytics_toolkit import sql

values = sql.execute_read(
    "gp",
    ["SELECT 1 AS id", "SELECT 2 AS id"],
    concurrency=2,
)
# values: [DataFrame({"id": [1]}), DataFrame({"id": [2]})]
```

[SQL functions index](index.md)
