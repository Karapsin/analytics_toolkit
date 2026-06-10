[SQL functions index](index.md)

# transfer

Stream data from a source SQL query into a target table on another configured connection.

```python
transfer(from_db: 'str', to_db: 'str', from_sql: 'str', to_table: 'str', replace_target_table: 'bool' = True, write_mode: 'str | None' = None, batch_size: 'int' = 100000, adaptive_batch_size: 'bool' = True, min_batch_size: 'int' = 1000, max_batch_size: 'int | None' = None, target_rows_per_second: 'bool' = True, target_batch_seconds: 'float' = 10.0, target_batch_memory_mb: 'float | None' = None, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, full_retry_cnt: 'int' = 5, full_timeout_increment: 'int | float' = 600, key_columns: 'list[str] | None' = None, gp_distributed_by_key: 'list[str] | None' = None, trino_insert_chunk_size: 'int | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str' = 'ReplicatedMergeTree', ch_cluster: 'str' = '{cluster}', ch_sharding_key: 'str' = 'rand()', ch_only_shard: 'bool' = False, ch_retry_per_host_drops: 'bool' = True, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None, progress: 'bool' = False, estimate_total_rows: 'bool' = False, table_schema: 'dict[str, str] | None' = None) -> 'int | SqlPlan | SqlOperationResult'
```

## Inputs

### General Inputs

- `from_db` - source connection key or alias
- `to_db` - target connection key or alias
- `from_sql` - source SQL query used by a transfer
- `to_table` - target table name
- `replace_target_table` - whether transfer should replace the target table using historical replace behavior
- `write_mode` - explicit write behavior: append, replace, or truncate_insert. `upsert` is reserved and currently unsupported
- `batch_size` - initial number of rows fetched and inserted per transfer batch
- `adaptive_batch_size` - whether transfer batch size should adapt after successful inserts
- `min_batch_size` - minimum adaptive transfer batch size
- `max_batch_size` - maximum adaptive transfer batch size; `None` leaves the default behavior in place
- `target_rows_per_second` - optimize batch size by transfer throughput (`rows / second`) when `True`
- `target_batch_seconds` - target insert duration used by time-based adaptive batching
- `target_batch_memory_mb` - approximate in-process memory target used for adaptive transfer batches
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
- Throughput-driven adaptation is enabled by default through
  `target_rows_per_second=True`; set it to `False` to use
  `target_batch_seconds`-based adaptation instead.

[SQL functions index](index.md)
