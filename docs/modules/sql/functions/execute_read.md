[SQL functions index](index.md)

# execute_read

Run setup SQL statements, then read the final SQL statement into a dataframe on the same connection.

```python
execute_read(db_key: 'str', query: 'str', print_queries: 'bool' = False, gp_break_query: 'bool' = False, gp_commit_each_statement: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, return_metadata: 'bool' = False, progress: 'bool' = False) -> 'pd.DataFrame | SqlOperationResult'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias from `.connections`; backend dispatch is selected from that entry
- `query` - text of SQL to execute or read
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
- Timing logs label setup execution as `[setup]` and the final dataframe query as `[read]`.

[SQL functions index](index.md)
