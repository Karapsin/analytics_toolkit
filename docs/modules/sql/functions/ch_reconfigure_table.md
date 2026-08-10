[SQL functions index](index.md)

# ch_reconfigure_table

Reconfigure a ClickHouse MergeTree table or toolkit-managed Distributed/`_shard`
pair while preserving its data. Physical shard DDL, Distributed facade DDL, and
Distributed routing are controlled independently.

```python
ch_reconfigure_table(db_key: 'str', table: 'str', *, ch_engine: 'str | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_sharding_key: 'str | None' = None, ch_distributed_table: 'bool | None' = None, ch_distributed_engine_template: 'str | None' = None, ch_distributed_cluster: 'str | None' = None, ch_shard_on_cluster: 'str | None' = None, ch_distributed_on_cluster: 'str | None' = None, ch_settings: 'Mapping[str, str | int | float | bool | None] | None' = None, ch_ddl_wait_policy: 'str | None' = None, reset_partition_by: 'bool' = False, reset_order_by: 'bool' = False, to_defaults: 'bool' = False, validate_row_count: 'bool' = True, retry_cnt: 'int' = 5, timeout_increment: 'float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False) -> 'SqlPlan | SqlOperationResult | None'
```

## Inputs

- `db_key` - configured ClickHouse connection key or alias
- `table` - MergeTree table or managed Distributed table to reconfigure
- `ch_ddl_wait_policy` - select shard/distributed replacement readiness checks; explicit values override the ClickHouse connection policy
- `ch_engine` - replacement MergeTree-family engine expression
- `partition_by` - replacement partition expression or partition columns
- `order_by` - replacement sorting expression or sorting columns
- `ch_sharding_key` - replacement Distributed sharding expression
- `ch_distributed_table` - preserve topology when omitted, convert to a managed pair when true, or convert to a physical table when false
- `ch_distributed_engine_template` - replacement `Distributed(...)` template using the configured ClickHouse placeholders
- `ch_distributed_cluster` - routing cluster stored inside the Distributed engine
- `ch_shard_on_cluster` - execution or destination cluster for physical shard DDL
- `ch_distributed_on_cluster` - complete management scope for Distributed facade DDL
- `ch_settings` - MergeTree settings to modify; a `None` value resets that setting
- `reset_partition_by` - remove the partition expression instead of replacing it
- `reset_order_by` - replace the sorting key with `tuple()`
- `to_defaults` - converge topology, engines, placements, routing, template, and sharding to `ddl_defaults.regular`; explicit arguments override defaults
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
    ch_shard_on_cluster="core",
    ch_distributed_cluster="core",
    ch_distributed_on_cluster="{cluster}",
    ch_engine="ReplicatedMergeTree",
    partition_by="toYYYYMM(event_date)",
    order_by=["event_date", "event_id"],
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
- shard metadata is read through the Distributed routing cluster when the `_shard` table is not local to the configured connection
- changing or removing an existing Distributed facade requires `ch_distributed_on_cluster`, because ClickHouse does not retain the original `ON CLUSTER` scope
- `to_defaults=True` treats the connection's regular ClickHouse DDL defaults as authoritative and fails before DDL when required defaults are missing
- local/pair conversion and physical-cluster relocation must be performed as separate operations
- cross-cluster migration requires a managed Distributed/`_shard` pair and both cluster names must be visible from the configured connection
- Atomic and Shared databases use `EXCHANGE TABLES`; other database engines use a rename fallback with automatic rollback
- successful validation removes the old table data; cleanup failures are reported through `SqlOperationResult.data`
- dry runs query ClickHouse metadata because the plan depends on the current table DDL and topology

[SQL functions index](index.md)
