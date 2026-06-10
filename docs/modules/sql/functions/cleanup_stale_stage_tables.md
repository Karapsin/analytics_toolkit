[SQL functions index](index.md)

# cleanup_stale_stage_tables

Drop stale transfer staging tables for a target table on the configured backend.

```python
cleanup_stale_stage_tables(db_key: 'str', target_table: 'str', stage_tables: 'Sequence[str] | None' = None, read_retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None) -> 'None'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias that owns the staging tables
- `target_table` - target table name whose staging tables should be cleaned
- `stage_tables` - explicit fully-qualified or unqualified stage table names to drop; empty to use discovery
- `read_retry_cnt` - number of retries for staging-table discovery and drops
- `timeout_increment` - delay increment used between cleanup retries
- `query_label` - safe label added to generated SQL comments and logs

## Usage

```python
from analytics_toolkit import sql

sql.cleanup_stale_stage_tables(
    db_key="gp",
    target_table="analytics.events",
)
```

Output example:

```python
sql.cleanup_stale_stage_tables(
    db_key="gp",
    target_table="analytics.events",
)
# None
```

## Notes

- Uses `transfer_staging_schema` and target user metadata from `.connections` to find stage tables.
- Discovery and drops use retry behavior with `read_retry_cnt` and `timeout_increment`.
- If staging cleanup cannot run because `transfer_staging_schema` is not configured, a one-time warning is emitted per process.

[SQL functions index](index.md)
