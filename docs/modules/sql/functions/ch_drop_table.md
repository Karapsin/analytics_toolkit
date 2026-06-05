[SQL functions index](index.md)

# ch_drop_table

Drop a ClickHouse distributed table and its managed shard table.

```python
ch_drop_table(db_key: 'str', table: 'str', *, ch_cluster: 'str | None' = '{cluster}', shard_table: 'str | None' = None, wait_for_absence: 'bool' = False, wait_timeout_seconds: 'int' = 300, wait_poll_interval_seconds: 'float' = 1, ch_retry_per_host_drops: 'bool' = True, ch_retry_per_host_drops_concurrency: 'int | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None) -> 'SqlPlan | SqlOperationResult | None'
```

## Inputs

- `db_key`: Connection key or alias from `.connections`.
- `table`: Table name to inspect, modify, or use for partition operations.
- `ch_cluster`: ClickHouse cluster name or macro for distributed/shard DDL; `None` skips cluster DDL where supported.
- `shard_table`: Explicit ClickHouse shard table name to drop with a distributed table.
- `wait_for_absence`: Whether ClickHouse drop should wait until target tables disappear from metadata.
- `wait_timeout_seconds`: Maximum wait time for ClickHouse table absence.
- `wait_poll_interval_seconds`: Polling interval while waiting for ClickHouse table absence.
- `ch_retry_per_host_drops`: Whether ClickHouse replace/drop flows may retry direct local drops on affected hosts.
- `ch_retry_per_host_drops_concurrency`: Maximum concurrent ClickHouse per-host cleanup connections; `None` uses the helper default.
- `dry_run`: When `True`, return a plan without mutating the database.
- `return_sql`: When `True`, return a `SqlPlan` instead of mutating a database.
- `return_metadata`: When `True`, return `SqlOperationResult` instead of the historical bare value.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.

## Notes

- ClickHouse-only helper.

[SQL functions index](index.md)
