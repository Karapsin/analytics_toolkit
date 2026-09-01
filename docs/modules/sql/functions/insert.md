[SQL functions index](index.md)

# insert

Insert one query result into an existing table on the same connection. Columns
are matched by position; the helper does not add a target column list or remap
columns by name.

```python
insert(db_key: 'str', table_name: 'str', query: 'str', *, print_queries: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, retry_policy: 'ExecuteRetryPolicy' = 'safe') -> 'int | SqlPlan | SqlOperationResult'
```

## Inputs

- `db_key` - connection key or alias used for both the query and insert
- `table_name` - existing target table
- `query` - exactly one `SELECT` whose columns match the target by position
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

[SQL functions index](index.md)
