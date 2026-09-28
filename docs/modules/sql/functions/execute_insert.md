[SQL functions index](index.md)

# execute_insert

Execute zero or more setup statements and insert the final `SELECT` result into
an existing table. Every statement uses the connection selected by `db_key`.

```python
execute_insert(db_key: 'str', table_name: 'str | list[str]', query: 'str | list[str]', *, print_queries: 'bool' = False, gp_break_query: 'bool' = False, gp_commit_each_statement: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, progress: 'bool' = False, retry_policy: 'ExecuteRetryPolicy' = 'safe', concurrency: 'int' = 1, soft_concurrency_cap: 'int | None' = None, hard_concurrency_cap: 'int' = 5) -> 'int | SqlPlan | SqlOperationResult | list[int | SqlPlan | SqlOperationResult]'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias used for every statement
- `table_name` - existing target table; use one table for all queries or a list matching the query list
- `query` - setup statements followed by one final `SELECT`; a non-empty list runs independent query strings and returns a list in input order
- `concurrency` - requested workers for list input; defaults to `1` and has no effect on string input
- `soft_concurrency_cap` - optional lower ceiling on requested list workers
- `hard_concurrency_cap` - safety ceiling for effective list workers; defaults to `5` and rejects higher effective concurrency
- `print_queries` - whether to print submitted SQL
- `retry_cnt` - maximum whole-operation attempts
- `timeout_increment` - delay increment between retries
- `query_label` - safe label added to SQL comments, plans, metadata, and logs
- `dry_run` - whether to return an ordered plan without executing SQL
- `return_sql` - whether to return the same ordered plan instead of executing SQL
- `return_metadata` - whether to wrap the affected-row count and plan in `SqlOperationResult`
- `progress` - whether to show multi-statement execution progress
- `retry_policy` - `safe`, `always`, or `never` mutation replay policy

### Backend-Specific Inputs

- `gp_break_query` - whether Greenplum should split and execute statements separately
- `gp_commit_each_statement` - whether Greenplum should commit each split statement

## Usage

```python
from analytics_toolkit import sql

sql.execute_insert(
    "trino",
    "mart.daily_scores",
    """
    CREATE TABLE scratch.current_scores AS SELECT * FROM raw.scores;
    SELECT user_id, score FROM scratch.current_scores
    """,
    dry_run=True,
)
```

Output excerpt:

```text
setup: CREATE TABLE scratch.current_scores AS ...
insert_target: INSERT INTO mart.daily_scores SELECT user_id, score ...
```

The final insert is positional. `dry_run=True` or `return_sql=True` returns an
ordered `SqlPlan` with `setup` and `insert_target` phases.

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

values = sql.execute_insert(
    "gp",
    ["sandbox.first", "sandbox.second"],
    ["SELECT 1 AS id", "SELECT 2 AS id"],
    concurrency=2,
)
# values: [1, 1] when the backend reports one affected row per item
```

[SQL functions index](index.md)
