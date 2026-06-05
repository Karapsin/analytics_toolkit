[SQL module index](index.md)

# Opt-In Write Controls

Write-heavy helpers keep their existing defaults. `load_df(append=False)` still
replaces the target, and `load_df(append=True)` still appends. New callers can
use `write_mode` for explicit behavior:

- `append`: insert rows into the target.
- `replace`: recreate or clear the target using the helper's historical replace
  behavior.
- `truncate_insert`: clear existing table data and then insert rows when the
  target exists.
- `upsert`: reserved and currently rejected for all backends.

`execute_sql`, `load_df`, `transfer_table`, `create_table_from_sql`,
`create_sql_table`, and `drop_many_partitions` accept `dry_run=True` or
`return_sql=True` to return a `SqlPlan` without mutating a database. The
backend-specific helpers `gp_create_many_partitions`, `ch_create_table_as`,
`ch_drop_table`, and `ch_full_table_move` support the same plan path. Plans
contain ordered SQL statements, aliases/backends, target metadata, and notable
options. Operations that require live inspection for exact SQL, such as
`ch_full_table_move`, use deterministic placeholder steps and mark the
inspection dependency in `SqlPlan.options` instead of opening a connection.

Use `format_plan` when you want a readable string for a dry-run plan:

```python
plan = sql.transfer(
    from_db="trino",
    to_db="gp_sandbox",
    from_sql="select * from iceberg.events.daily",
    to_table="sandbox.events_daily",
    dry_run=True,
)

print(sql.format_plan(plan, max_sql_chars=120))
print(sql.format_plan(plan, include_sql=False))
```

`read_sql`, `execute_sql`, `execute_read`, `load_df`, `transfer_table`,
`create_table_from_sql`, `create_sql_table`, and `drop_many_partitions` also
accept `return_metadata=True`. The backend-specific helpers
`ch_create_table_as`, `ch_drop_table`, and `ch_full_table_move` support it too.
The returned `SqlOperationResult` includes row counts when available plus
metadata such as elapsed seconds, retry attempts, statement count, operation
status, and query label. Historical default return values are unchanged. SQL
timing logs render elapsed durations in human-readable units, for example
`1 minute 30 seconds`; metadata continues to store numeric seconds.

Use `query_label` to add a safe SQL comment to generated statements and logs:

```python
plan = sql.load_df(
    "gp",
    "sandbox.scores",
    scores_df,
    write_mode="truncate_insert",
    dry_run=True,
    query_label="daily_score_refresh",
)

loaded = sql.load_df(
    "gp",
    "sandbox.scores",
    scores_df,
    return_metadata=True,
    query_label="daily_score_refresh",
)
loaded.rows
loaded.metadata.final_target_rows

planned_execute = sql.execute(
    "trino",
    "delete from sandbox.old_rows",
    dry_run=True,
    query_label="cleanup_old_rows",
)

read_result = sql.read(
    "gp",
    "select * from sandbox.scores",
    return_metadata=True,
)
scores_df = read_result.data
read_result.metadata.elapsed_seconds
```

Use `table_info` for lightweight live inspection. Row counting is opt-in
because it executes a `COUNT(*)`/`count()` scan on the target table.

```python
info = sql.table_info("trino", "events")
info.exists
info.resolved_table
info.columns

counted = sql.table_info("gp", "sandbox.scores", include_row_count=True)
counted.row_count

columns_df = counted.to_frame()
```

[SQL module index](index.md)
