[SQL functions index](index.md)

# drop_tables

Drop one table or a list of tables using the configured backend.

```python
drop_tables(db_key: 'str', table: 'str | list[str]', *, if_exists: 'bool' = False, ch_cluster: 'str | None' = '{cluster}', ch_drop_shard: 'bool' = True, ch_drop_distributed: 'bool' = True, ch_wait_for_absence: 'bool' = False, ch_wait_timeout_seconds: 'int' = 300, ch_wait_poll_interval_seconds: 'float' = 1, ch_retry_per_host_drops: 'bool' = True, ch_retry_per_host_drops_concurrency: 'int | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None) -> 'SqlPlan | SqlOperationResult | None'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias from `.connections`
- `table` - table name or list of table names to drop in input order
- `if_exists` - when `True`, generate `DROP TABLE IF EXISTS`; otherwise generate plain `DROP TABLE`
- `dry_run` - when `True`, return a plan without mutating the database
- `return_sql` - when `True`, return a `SqlPlan` instead of mutating a database
- `return_metadata` - when `True`, return `SqlOperationResult` instead of the historical bare value
- `query_label` - safe label added to generated SQL comments, plans, metadata, and logs

### Backend-Specific Inputs

- `ch_cluster` - clickHouse cluster name or macro for distributed/shard DDL; `None` skips cluster DDL where supported
- `ch_drop_distributed` - whether ClickHouse distributed tables should be dropped
- `ch_drop_shard` - whether ClickHouse shard tables should be dropped
- `ch_retry_per_host_drops` - whether ClickHouse replace/drop flows may retry direct local drops on affected hosts
- `ch_retry_per_host_drops_concurrency` - maximum concurrent ClickHouse per-host cleanup connections; `None` uses the helper default
- `ch_wait_for_absence` - whether ClickHouse drop should wait until target tables disappear from metadata
- `ch_wait_timeout_seconds` - maximum wait time for ClickHouse table absence
- `ch_wait_poll_interval_seconds` - polling interval while waiting for ClickHouse table absence

## Usage

```python
from analytics_toolkit import sql

plan = sql.drop_tables(
    "ch",
    ["sandbox.events_daily", "sandbox.events_archive"],
    if_exists=True,
    dry_run=True,
)
print(sql.format_plan(plan))
```

Output example:

```text
SQL plan: drop_tables
- ch: DROP TABLE IF EXISTS sandbox.events_daily
- ch: DROP TABLE IF EXISTS sandbox.events_archive
```

## Notes

- ClickHouse-specific flags control whether the distributed table, shard table, or both are dropped.
- Plain `DROP TABLE` is the default; set `if_exists=True` to keep the historical safe-drop form.

[SQL functions index](index.md)
