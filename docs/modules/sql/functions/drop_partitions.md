[SQL functions index](index.md)

# drop_partitions

Drop partition values from one table using the configured backend.

```python
drop_partitions(db_key: 'str', table: 'str', partition_keys_list: 'list[str]', trino_partition_column: 'str | None' = None, gp_truncate: 'bool' = False, *, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False) -> 'SqlPlan | SqlOperationResult | None'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias from `.connections`
- `table` - target table name for the partition removal
- `partition_keys_list` - partition values to remove from the target table
- `retry_cnt` - number of operation retries with fresh connections
- `timeout_increment` - delay increment used between operation retries
- `dry_run` - when `True`, return a plan without mutating the database
- `return_sql` - when `True`, return a `SqlPlan` instead of mutating a database
- `return_metadata` - when `True`, return `SqlOperationResult` instead of the historical bare value
- `query_label` - safe label added to generated SQL comments, plans, metadata, and logs

### Backend-Specific Inputs

- `gp_truncate` - for Greenplum partition removal, truncate matching partitions instead of dropping them
- `trino_partition_column` - partition column used by Trino partition deletes

## Usage

```python
from analytics_toolkit import sql

plan = sql.drop_partitions(
    "gp",
    "sandbox.events",
    ["2026-06-01", "2026-06-02"],
    dry_run=True,
)
print(sql.format_plan(plan))
```

Output example:

```text
SQL plan: drop_partitions
- gp: ALTER TABLE sandbox.events DROP PARTITION ...
```

## Notes

- Greenplum, Trino, and ClickHouse use different generated SQL for partition removal.

[SQL functions index](index.md)
