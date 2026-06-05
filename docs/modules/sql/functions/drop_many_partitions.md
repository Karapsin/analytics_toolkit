[SQL functions index](index.md)

# drop_many_partitions

Drop or truncate several partition values from one target table.

```python
drop_many_partitions(db_key: 'str', table: 'str', partition_keys_list: 'list[str]', partition_column: 'str | None' = None, gp_truncate: 'bool' = False, *, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False) -> 'SqlPlan | SqlOperationResult | None'
```

## Inputs

### General Inputs

- `db_key`: Connection key or alias from `.connections`.
- `table`: Table name to inspect, modify, or use for partition operations.
- `partition_keys_list`: Partition values to remove from the target table.
- `retry_cnt`: Number of operation retries with fresh connections.
- `timeout_increment`: Delay increment used between operation retries.
- `dry_run`: When `True`, return a plan without mutating the database.
- `return_sql`: When `True`, return a `SqlPlan` instead of mutating a database.
- `return_metadata`: When `True`, return `SqlOperationResult` instead of the historical bare value.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.

### Backend-Specific Inputs

- `gp_truncate`: For Greenplum partition removal, truncate matching partitions instead of dropping them.
- `partition_column`: Partition column used by backends that need it for partition removal.

## Usage

```python
from analytics_toolkit import sql

plan = sql.drop_many_partitions(
    "gp",
    "sandbox.events",
    ["2026-06-01", "2026-06-02"],
    dry_run=True,
)
print(sql.format_plan(plan))
```

## Notes

- Greenplum, Trino, and ClickHouse use different generated SQL for partition removal.

[SQL functions index](index.md)
