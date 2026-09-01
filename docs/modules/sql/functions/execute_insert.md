[SQL functions index](index.md)

# execute_insert

Execute zero or more setup statements and insert the final `SELECT` result into
an existing table. Every statement uses the connection selected by `db_key`.

```python
execute_insert(db_key: 'str', table_name: 'str', query: 'str', *, print_queries: 'bool' = False, gp_break_query: 'bool' = False, gp_commit_each_statement: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, progress: 'bool' = False, retry_policy: 'ExecuteRetryPolicy' = 'safe') -> 'int | SqlPlan | SqlOperationResult'
```

## Inputs

- `db_key` - connection key or alias used for every statement
- `table_name` - existing target table
- `query` - setup statements followed by one final `SELECT`
- `print_queries` - whether to print submitted SQL
- `gp_break_query` - whether Greenplum should split and execute statements separately
- `gp_commit_each_statement` - whether Greenplum should commit each split statement
- `retry_cnt` - maximum whole-operation attempts
- `timeout_increment` - delay increment between retries
- `query_label` - safe label added to SQL comments, plans, metadata, and logs
- `dry_run` - whether to return an ordered plan without executing SQL
- `return_sql` - whether to return the same ordered plan instead of executing SQL
- `return_metadata` - whether to wrap the affected-row count and plan in `SqlOperationResult`
- `progress` - whether to show multi-statement execution progress
- `retry_policy` - `safe`, `always`, or `never` mutation replay policy

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

[SQL functions index](index.md)
