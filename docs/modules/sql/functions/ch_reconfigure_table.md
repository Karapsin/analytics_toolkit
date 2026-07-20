[SQL functions index](index.md)

# ch_reconfigure_table

Reconfigure a ClickHouse MergeTree table or toolkit-managed Distributed/`_shard`
pair while preserving its data.

```python
ch_reconfigure_table(db_key: 'str', table: 'str', *, ch_engine: 'str | None' = None, ch_partition_by: 'Sequence[str] | str | None' = None, ch_order_by: 'Sequence[str] | str | None' = None, ch_cluster: 'str | None' = None, ch_source_cluster: 'str | None' = None, ch_sharding_key: 'str | None' = None, ch_settings: 'Mapping[str, str | int | float | bool | None] | None' = None, ch_reset_partition_by: 'bool' = False, ch_reset_order_by: 'bool' = False, validate_row_count: 'bool' = True, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False) -> 'SqlPlan | SqlOperationResult | None'
```

## Inputs

- `db_key` - configured ClickHouse connection key or alias
- `table` - MergeTree table or managed Distributed table to reconfigure
- `ch_engine` - replacement MergeTree-family engine expression
- `ch_partition_by` - replacement partition expression or partition columns
- `ch_order_by` - replacement sorting expression or sorting columns
- `ch_cluster` - destination data cluster and Distributed routing cluster
- `ch_source_cluster` - source cluster override or validation value when it cannot be inferred
- `ch_sharding_key` - replacement Distributed sharding expression
- `ch_settings` - MergeTree settings to modify; a `None` value resets that setting
- `ch_reset_partition_by` - remove the partition expression instead of replacing it
- `ch_reset_order_by` - replace the sorting key with `tuple()`
- `validate_row_count` - require stable source and matching replacement counts before cutover
- `retry_cnt` - number of attempts using fresh connections
- `timeout_increment` - delay increment between retry attempts
- `query_label` - safe label added to planned and executed SQL
- `dry_run` - inspect the live table and return its `SqlPlan` without changing it
- `return_sql` - return the same live, read-only plan instead of executing it
- `return_metadata` - return `SqlOperationResult` with DDL, strategy, counts, and cleanup status

## Usage

```python
from analytics_toolkit import sql

plan = sql.ch_reconfigure_table(
    "ch",
    "sandbox.events",
    ch_cluster="core",
    ch_engine="ReplicatedMergeTree",
    ch_partition_by="toYYYYMM(event_date)",
    ch_order_by=["event_date", "event_id"],
    dry_run=True,
)
print(sql.format_plan(plan))
```

Output example:

```text
SqlPlan: ch_reconfigure_table
Target: alias=ch backend=ch table=sandbox.events
Statements:
  [create_replacement] CREATE TABLE IF NOT EXISTS sandbox.events_shard__reconfigure_...
  [copy_data] INSERT INTO sandbox.events__reconfigure_... SELECT * FROM sandbox.events
  [cutover] EXCHANGE TABLES sandbox.events_shard AND sandbox.events_shard__reconfigure_...
```

## Notes

- pause writers for the entire rebuild or migration; the helper aborts when it observes source count drift but cannot create a transactional snapshot across tables
- setting-only changes use direct `ALTER TABLE ... MODIFY/RESET SETTING`; engine, partition, and order changes use a staged rebuild
- cluster macro and literal names are resolved before comparison; disjoint clusters can be migrated, while partially overlapping clusters are rejected
- cross-cluster migration requires a managed Distributed/`_shard` pair and both cluster names must be visible from the configured connection
- Atomic and Shared databases use `EXCHANGE TABLES`; other database engines use a rename fallback with automatic rollback
- successful validation removes the old table data; cleanup failures are reported through `SqlOperationResult.data`
- dry runs query ClickHouse metadata because the plan depends on the current table DDL and topology

[SQL functions index](index.md)
