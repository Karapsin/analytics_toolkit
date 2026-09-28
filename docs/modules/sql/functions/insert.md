[SQL functions index](index.md)

# insert

Insert one query result into an existing table on the same connection. Columns
are matched by position; the helper does not add a target column list or remap
columns by name.

```python
insert(db_key: 'str', table_name: 'str | list[str]', query: 'str | list[str]', *, print_queries: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, retry_policy: 'ExecuteRetryPolicy' = 'safe', concurrency: 'int' = 1, soft_concurrency_cap: 'int | None' = None, hard_concurrency_cap: 'int' = 5) -> 'int | SqlPlan | SqlOperationResult | list[int | SqlPlan | SqlOperationResult]'
```

## Inputs

- `db_key` - connection key or alias used for both the query and insert
- `table_name` - existing target table; use one table for all queries or a list matching the query list
- `query` - exactly one `SELECT` whose columns match the target by position; a non-empty list runs independent query strings and returns a list in input order
- `concurrency` - requested workers for list input; defaults to `1` and has no effect on string input
- `soft_concurrency_cap` - optional lower ceiling on requested list workers
- `hard_concurrency_cap` - safety ceiling for effective list workers; defaults to `5` and rejects higher effective concurrency
- `print_queries` - whether to print submitted SQL
- `retry_cnt` - maximum whole-operation attempts
- `timeout_increment` - delay increment between retries
- `query_label` - safe label added to SQL comments, plans, metadata, and logs
- `dry_run` - whether to return a plan without executing SQL
- `return_sql` - whether to return the same ordered plan instead of executing SQL
- `return_metadata` - whether to wrap the affected-row count and plan in `SqlOperationResult`
- `retry_policy` - `safe`, `always`, or `never` mutation replay policy

## Usage

```python
from analytics_toolkit import sql

inserted = sql.insert(
    "gp",
    "mart.daily_scores",
    "SELECT user_id, score FROM staging.daily_scores",
)

inserted
# 42
```

The return value is the backend-reported affected-row count, or `0` when the
backend does not expose it. The final statement must be a `SELECT`; use
`execute_insert` when setup statements are needed.

## Independent Query Batches

Each list item uses its own connection. Setup statements and the final operation
within that item stay sequential. Items must be independent; temporary tables
and session state are not shared between items. Options apply to every item.
All items are validated before any execution starts. A failed batch raises
`SqlBatchExecutionError` with successful, failed, ambiguous, and cancelled item
outcomes; completed writes are not rolled back across the batch.

`dry_run=True` and `return_sql=True` return ordered lists of plans.

```python
from analytics_toolkit import sql

values = sql.insert(
    "gp",
    ["sandbox.first", "sandbox.second"],
    ["SELECT 1 AS id", "SELECT 2 AS id"],
    concurrency=2,
)
# values: [1, 1] when the backend reports one affected row per item
```

[SQL functions index](index.md)
