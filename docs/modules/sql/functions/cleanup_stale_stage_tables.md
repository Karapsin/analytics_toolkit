[SQL functions index](index.md)

# cleanup_stale_stage_tables

Drop stale transfer staging tables on the configured backend.

```python
cleanup_stale_stage_tables(db_key: 'str', target_table: 'str | None' = None, stage_tables: 'Sequence[str] | None' = None, clean_all: 'bool' = False, read_retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None) -> 'None'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias that owns the staging tables
- `target_table` - target table name whose staging tables should be cleaned; required for target-scoped discovery and optional for `clean_all=True` or explicit `stage_tables`
- `stage_tables` - explicit fully-qualified or unqualified stage table names to drop; `None` uses discovery and an empty sequence drops nothing
- `clean_all` - when `True`, drop all Analytics Toolkit stage tables in `transfer_staging_schema` for the configured connection user, across all target tables
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

sql.cleanup_stale_stage_tables(
    db_key="gp",
    clean_all=True,
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

- Supported concurrency is limited to workers created by one `sql.transfer`
  call. Independent simultaneous calls to the same destination are unsupported;
  cleanup is best effort and is not a distributed lock or cross-backend fence.
- New-format automatic transfer cleanup verifies the full transfer ID and exact
  stored canonical destination. A destination-hash prefix match alone never
  authorizes deletion, and no atomic “new transfer wins” behavior is promised.
- Empty, malformed, or unverifiable new-format stages are preserved. Historical
  source snapshots on a different source connection may not be discoverable;
  pass explicit `stage_tables=[...]` or deliberately use `clean_all=True` for
  operator-authorized cleanup.
- This helper creates no manifest, lease, heartbeat, owner marker, bookkeeping,
  or other coordination table.
- Uses `transfer_staging_schema` and target user metadata from `.connections` to find stage tables.
- Passing `stage_tables=None` with `clean_all=False` discovers matching stale
  stage tables for `target_table`; passing `[]` drops nothing; passing explicit
  names drops only those requested tables.
- Passing `clean_all=True` discovers all user-owned toolkit stage tables in
  `transfer_staging_schema`, regardless of `target_table`; it cannot be combined
  with explicit `stage_tables`.
- Unqualified explicit names are resolved inside `transfer_staging_schema`; fully
  qualified explicit names are used unchanged.
- Discovery and drops use retry behavior with `read_retry_cnt` and `timeout_increment`.
- If staging cleanup cannot run because `transfer_staging_schema` is not configured, a one-time warning is emitted per process.

[SQL functions index](index.md)
