[SQL functions index](index.md)

# drop_tables

Drop one table or a list of tables using the configured backend.

```python
drop_tables(db_key: 'str', table: 'str | list[str]', *, if_exists: 'bool' = False, ch_cluster: 'str | None' = '{cluster}', ch_drop_shard: 'bool' = True, ch_drop_distributed: 'bool' = True, ch_wait_for_absence: 'bool' = False, ch_wait_timeout_seconds: 'int' = 300, ch_wait_poll_interval_seconds: 'float' = 1, ch_retry_per_host_drops: 'bool' = True, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None, ch_unavailable_shards: 'str | None' = None) -> 'SqlPlan | SqlOperationResult | None'
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

- `ch_unavailable_shards` - ClickHouse outage policy: `None` uses the connection default (`stop` when omitted); `healthy_shards` enables ad hoc partial work with prominent warnings; see [outage policies](../clickhouse-distributed-tables.md#unavailable-shards)

- `ch_cluster` - ClickHouse inspection scope name or macro; `None` restricts selected resources to the coordinator's local scope
- `ch_drop_distributed` - whether ClickHouse distributed tables should be dropped
- `ch_drop_shard` - whether ClickHouse shard tables should be dropped
- `ch_retry_per_host_drops` - whether ClickHouse replace/drop flows may retry direct local drops on affected hosts
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

The offline plan records unresolved execution requirements. It does not inspect
the existing pair or guess its DROP scopes.

## Notes

- ClickHouse-specific flags control whether the distributed table, shard table, or both are dropped.
- For an existing ClickHouse table, execution verifies the selected roles and
  their actual placement. A physical companion requires a verified storage
  relationship and matching column metadata. A facade-only drop does not require
  unselected storage to be available. A facade on
  one cluster can reference physical storage on another; each role uses its
  verified scope. Same-named local companions or remote logical tables are not
  selected merely because they match the caller's cluster or a `_shard` suffix.
- All selected resources and endpoints across the request are verified before
  the first DROP. Each existing endpoint resource requires a verifiable,
  non-nil table UUID and complete definition; unverifiable identities stop
  before DDL. A non-`None` `ch_cluster` cannot widen an already known role's
  placement. With `ch_cluster=None`, a selected remote or multi-host role rejects
  before DDL; a verified singleton coordinator alias can execute locally.
- Equivalent identifier spellings and cluster aliases drop the same resource
  once when its complete verified endpoint and table identities match.
- `if_exists=True` permits verified absent resources, including a partially
  occupied cluster scope, but still requires every selected endpoint to be
  available. Existing identities and observed absences are rechecked before
  DROP; missing metadata is not treated as permission to guess a resource.
- Absence waits and per-host recovery retain each selected role's table and
  endpoint scope. Recovery rechecks the verified table and server identities,
  retaining the discovered native host/port identity. HTTP drops reject multiple
  discovered native instances sharing one host; use independently addressable
  endpoints or the native driver. Unrelated same-named resources are not used
  for absence checks.
- Offline `dry_run`/`return_sql` plans open no connection and leave
  `pending_existing_layout_verification=True` and
  `pending_resource_scope_resolution=True` in `plan.options`. Their execution
  requirements remain unresolved until actual resource verification; they do
  not provide a certified DROP statement list or count.
- Plain `DROP TABLE` is the default; set `if_exists=True` to keep the historical safe-drop form.
- `ch_wait_timeout_seconds` must be a built-in positive integer, and
  `ch_wait_poll_interval_seconds` must be a finite positive real number. These
  values are validated even when returning a dry-run plan.

[SQL functions index](index.md)
