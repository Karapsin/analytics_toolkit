[SQL functions index](index.md)

# transfer

Stream data from a source SQL query into a target table on another configured connection.

```python
transfer(from_db: 'str', to_db: 'str', from_sql: 'str', to_table: 'str', write_mode: 'str | None' = None, batch_size: 'int' = 100000, adaptive_batch_size: 'bool' = True, min_batch_size: 'int' = 1000, max_batch_size: 'int | None' = None, target_rows_per_second: 'bool' = True, target_rows_per_second_window: 'int' = 5, target_rows_per_second_deadband: 'float' = 0.15, target_batch_seconds: 'float | None' = None, min_batch_seconds: 'float | None' = None, max_batch_seconds: 'float | None' = None, target_batch_memory_mb: 'float | None' = None, min_batch_memory_mb: 'float | None' = None, max_batch_memory_mb: 'float | None' = None, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, full_retry_cnt: 'int' = 5, full_timeout_increment: 'int | float' = 600, key_columns: 'list[str] | None' = None, gp_distributed_by_key: 'list[str] | None' = None, gp_insert_chunk_size: 'int | None' = None, trino_insert_chunk_size: 'int | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str' = 'ReplicatedMergeTree', ch_cluster: 'str' = '{cluster}', ch_sharding_key: 'str' = 'rand()', ch_only_shard: 'bool' = False, ch_retry_per_host_drops: 'bool' = True, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None, progress: 'bool' = False, estimate_total_rows: 'bool' = False, table_schema: 'dict[str, str] | None' = None) -> 'int | SqlPlan | SqlOperationResult'
```

## Inputs

### General Inputs

- `from_db` - source connection key or alias
- `to_db` - target connection key or alias
- `from_sql` - source SQL query used by a transfer
- `to_table` - target table name
- `write_mode` - explicit write behavior: append, replace, or truncate_insert. `upsert` is reserved and currently unsupported
- `batch_size` - initial number of rows fetched and inserted per transfer batch
- `adaptive_batch_size` - whether transfer batch size should adapt after successful inserts
- `min_batch_size` - minimum adaptive transfer batch size
- `max_batch_size` - maximum adaptive transfer batch size; `None` leaves the default behavior in place
- `target_rows_per_second` - optimize batch size by transfer throughput (`rows / second`) when `True`
- `target_rows_per_second_window` - number of recent successful throughput samples to average for throughput adaptation decisions
- `target_rows_per_second_deadband` - minimum relative throughput change needed to trigger throughput adaptation
- `target_batch_seconds` - target insert duration used by time-based adaptive batching
- `min_batch_seconds` - minimum allowed value for time-based adaptive targets when enabled
- `max_batch_seconds` - maximum allowed value for time-based adaptive targets when enabled
- `target_batch_memory_mb` - approximate in-process memory target used for adaptive transfer batches
- `min_batch_memory_mb` - minimum allowed value for memory-based adaptive targets when enabled
- `max_batch_memory_mb` - maximum allowed value for memory-based adaptive targets when enabled
- `key_columns` - columns used to validate staged rows against an existing target before final insert
- `retry_cnt` - number of operation retries with fresh connections
- `timeout_increment` - delay increment used between operation retries
- `full_retry_cnt` - number of retries for the whole transfer flow after a transfer-level failure
- `full_timeout_increment` - delay increment used between full transfer retries
- `dry_run` - when `True`, return a plan without mutating the database
- `return_sql` - when `True`, return a `SqlPlan` instead of mutating a database
- `return_metadata` - when `True`, return `SqlOperationResult` instead of the historical bare value
- `query_label` - safe label added to generated SQL comments, plans, metadata, and logs
- `progress` - whether to show progress bars for supported multi-step or row-loading operations
- `estimate_total_rows` - whether transfer should ask the source backend for a best-effort row estimate for progress
- `table_schema` - explicit backend-native column type mapping for created tables
- `partition_by` - partitioning columns or expression for created tables, interpreted according to the target backend
- `order_by` - ordering or sorting columns or expression for created tables, interpreted according to the target backend

### Backend-Specific Inputs

- `gp_distributed_by_key` - distribution key columns for created Greenplum target tables
- `gp_insert_chunk_size` - dataframe/row insert page size for Greenplum transfer stage inserts
- `trino_insert_chunk_size` - number of rows per Trino parameterized multi-row insert statement
- `ch_engine` - engine to use for created ClickHouse local shard tables
- `ch_cluster` - cluster name or macro for ClickHouse distributed/shard DDL; `None` skips cluster DDL where supported
- `ch_sharding_key` - sharding expression for ClickHouse distributed table creation
- `ch_only_shard` - for ClickHouse, create or mutate only the local table instead of a distributed/shard pair
- `ch_retry_per_host_drops` - whether ClickHouse replace/drop flows may retry direct local drops on affected hosts

## Usage

```python
from analytics_toolkit import sql

rows = sql.transfer(
    from_db="trino",
    to_db="gp",
    from_sql="select order_id, user_id, amount from sandbox.orders",
    to_table="sandbox.orders_copy",
    batch_size=50_000,
)
```

Output example:

```python
rows
# 125000
```

## Notes

- Prefer this short entrypoint in user-facing examples.
- Retries restart the public operation with fresh connections.
- ClickHouse targets create distributed/shard table pairs unless `ch_only_shard=True`.
- `target_rows_per_second`, `target_batch_seconds`, and `target_batch_memory_mb`
  are mutually exclusive adaptation controls: set at most one per call.
- Throughput-driven adaptation is default via
  `target_rows_per_second=True` (or omitted).
- If you set `target_batch_seconds`, `target_rows_per_second` is disabled.
- If you set `target_batch_memory_mb`, memory-based adaptation is used and
  takes precedence.
- `transfer_staging_schema` configured on the target connection keeps staging
  tables inside that schema. When configured, stale and overlapping staging tables
  for the target user are cleaned before and after each transfer.
- `sql.cleanup_stale_stage_tables()` is the public helper for explicit cleanup of
  user-scoped staging tables.
- Optional `min_batch_seconds`, `max_batch_seconds`, `min_batch_memory_mb`, and
  `max_batch_memory_mb` clamp their corresponding active target constraints
  when those targets are enabled.
- Larger `gp_insert_chunk_size` values can reduce Greenplum insert round trips,
  but benchmark them against your target cluster and row shape.

[SQL functions index](index.md)
