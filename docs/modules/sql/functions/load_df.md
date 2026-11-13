[SQL functions index](index.md)

# load_df

Load a pandas dataframe into a SQL table on a configured backend.

```python
load_df(db_key: 'str', destination_table: 'str', df: 'pd.DataFrame', append: 'bool' = False, write_mode: 'str | None' = None, gp_distributed_by_key: 'str | Sequence[str] | None' = None, gp_partitions: 'Mapping[str, Any] | None' = None, key_columns: 'str | Sequence[str] | None' = None, upsert_partition_column: 'str | None' = None, retry_cnt: 'int' = 5, timeout_increment: 'float' = 5, trino_insert_chunk_size: 'int | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str | None' = None, ch_cluster: 'str | None' = None, ch_sharding_key: 'str | None' = None, ch_distributed_table: 'bool | None' = None, ch_distributed_engine_template: 'str | None' = None, ch_distributed_cluster: 'str | None' = None, ch_shard_on_cluster: 'str | None' = None, ch_distributed_on_cluster: 'str | None' = None, ch_ddl_ready_timeout_seconds: 'float | None' = None, ch_ddl_wait_policy: 'str | None' = None, ch_only_shard: 'bool' = False, ch_retry_per_host_drops: 'bool' = True, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None, gp_insert_chunk_size: 'int | None' = None, progress: 'bool' = False, table_schema: 'dict[str, str] | None' = None, empty_source_policy: 'EmptySourcePolicy | None' = None, *, ch_unavailable_shards: 'str | None' = None) -> 'int | SqlPlan | SqlOperationResult'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias from `.connections`; backend dispatch is selected from that entry
- `destination_table` - target table name for dataframe loading
- `df` - dataframe to load
- `append` - historical dataframe loading flag; `True` appends and `False` replaces unless `write_mode` is supplied
- `write_mode` - explicit write behavior: append, replace, truncate_insert, or upsert
- `empty_source_policy` - zero-row behavior: `replace`, `keep`, or `error`; omission resolves to `replace` for replace/truncate modes and `keep` for append/upsert
- `key_columns` - key column or columns used to validate staged rows and required when `write_mode="upsert"`; upserts prepend the partition column when supplied
- `upsert_partition_column` - staged column added to the effective upsert key on every backend; also defines affected partitions and is required for Trino and ClickHouse upserts
- `retry_cnt` - number of operation retries with fresh connections
- `timeout_increment` - delay increment used between operation retries
- `dry_run` - when `True`, return a plan without mutating the database
- `return_sql` - when `True`, return a `SqlPlan` instead of mutating a database
- `return_metadata` - when `True`, return `SqlOperationResult` instead of the historical bare value
- `query_label` - safe label added to generated SQL comments, plans, metadata, and logs
- `progress` - whether to show progress bars for supported multi-step or row-loading operations
- `table_schema` - explicit backend-native column type mapping for created tables
- `partition_by` - partitioning columns or expression for created tables, interpreted according to the target backend
- `order_by` - ordering or sorting columns or expression for created tables, interpreted according to the target backend

### Backend-Specific Inputs

- `ch_unavailable_shards` - ClickHouse outage policy: `None` uses the connection default (`stop` when omitted); `healthy_shards` enables ad hoc partial work with prominent warnings; see [outage policies](../clickhouse-distributed-tables.md#unavailable-shards)

- `gp_distributed_by_key` - distribution key column or columns for created Greenplum target tables
- `gp_partitions` - initial Greenplum range or list child definitions used when the final target is created or replaced
- `gp_insert_chunk_size` - dataframe insert page size for Greenplum
- `trino_insert_chunk_size` - number of rows per Trino parameterized multi-row insert statement
- `ch_engine` - engine to use for created ClickHouse local shard tables
- `ch_cluster` - deprecated compatibility shortcut for execution and routing clusters
- `ch_sharding_key` - sharding expression for ClickHouse distributed table creation
- `ch_distributed_table` - override for configured pair or single-table topology
- `ch_distributed_engine_template` - override for the configured Distributed engine template
- `ch_distributed_cluster` - routing cluster inside the Distributed engine
- `ch_shard_on_cluster` - execution cluster for shard DDL
- `ch_distributed_on_cluster` - execution cluster for Distributed facade DDL
- `ch_ddl_ready_timeout_seconds` - overall ClickHouse post-create readiness deadline; explicit values override the connection default
- `ch_ddl_wait_policy` - `wait_all`, `wait_shard`, `wait_distr`, or `wait_none`; explicit values override the ClickHouse connection policy
- `ch_only_shard` - for ClickHouse, create or mutate only the local table instead of a distributed/shard pair
- `ch_retry_per_host_drops` - whether ClickHouse replace/drop flows may retry direct local drops on affected hosts

## Usage

```python
import pandas as pd
from analytics_toolkit import sql

scores = pd.DataFrame({"user_id": [1, 2], "score": [10.5, 12.0]})
rows = sql.load_df(
    db_key="gp",
    destination_table="sandbox.scores",
    df=scores,
    write_mode="truncate_insert",
)
```

Output example:

```python
rows
# 2
```

## Notes

- Every SQL data-stage name created by `load_df` begins with the first 16 hex
  characters of SHA-256 over the exact canonical destination, followed by
  `__`. This stable prefix is collision-resistant but is not proof of identity.
  Existing names are never reused or overwritten; runtime collisions allocate
  another name, and cleanup retains the actual created name.
- Omitting `write_mode` selects `replace` unless the legacy `append=True` flag is supplied. Prefer an explicit `write_mode` for incremental or truncate-and-load workflows.
- Replacing an existing table builds and validates an offside table before cutover. Greenplum uses a transaction, Trino uses a reversible rename, and ClickHouse exchanges or reversibly renames the physical/facade replacement pair. New columns are included without dropping the live table first.
- Dataframe write column names must be unique as SQL strings. For modes that
  retain an existing target schema, unknown or noninsertable incoming fields fail
  before stage creation or target mutation. Backend-supported input columns remain
  available. Valid subset and reordered inputs remain supported
  for `append` and `truncate_insert`; replacement uses its new resolved schema.
- Nonempty `append`, `truncate_insert` and `upsert` inputs reject converted SQL
  nulls for `NOT NULL` columns and omitted required columns before target mutation.
  Backend-supported append/truncate omissions, complete-row upsert requirements
  and empty-input behavior are preserved.
- Python `Enum` members resolve through `.value`, recursively, before validation
  and native or text conversion. Underlying strings, exact numbers and nulls keep
  their normal conversion rules.
- Explicit or existing integer columns convert accepted boolean scalars to native
  `0`/`1`; automatic boolean and integer inference retain their respective types.
- An empty dataframe follows `empty_source_policy`. Empty replacement creates a zero-row table with the requested schema. Pass `table_schema` when an empty object-typed column has no unambiguous backend type.
- `write_mode="upsert"` stages incoming rows and rejects duplicate staged keys
  before any target mutation.
- Existing-target upserts require every writable column, including columns with
  defaults. Generated and noninsertable columns are excluded. Missing or extra
  columns fail before stage creation or target mutation; reordered complete rows
  are loaded in target order. Explicit `table_schema` and existing target types
  remain authoritative and are validated against the complete dataframe.
  When Python serializes values to text, their complete encoded strings must
  fit the authoritative widths before target creation or mutation.
- Existing ClickHouse upserts bind target preservation reads, partition mutation,
  final inserts and prepared-payload replay to the same verified physical storage
  scope; caller creation policy still controls new stages. Offline
  `dry_run`/`return_sql` plans open no connection and set
  `plan.options["pending_existing_layout_verification"] = True`. Their provisional
  partition SQL uses the requested policy until the existing layout is verified.
- ClickHouse timestamps use the [resolved write timezone](../dataframe-loading.md#backend-notes)
  without changing the declared column type. Explicit column zones win.
  Authoritative schemas reject ambiguous or nonexistent naive local times
  before mutation; inference preserves unresolved local times and mixed
  awareness as exact text. Aware and raw values retain their instant.
- When `upsert_partition_column` is supplied, the effective key is that column
  followed by `key_columns`, with duplicates removed. ID 7 in January and ID 7
  in February are separate records. Without a partition column, `key_columns`
  retains its usual meaning. Ordinary append key validation is unchanged.
- When `load_df` creates a target table that did not exist at the start and the
  load later fails, it drops that newly created target during cleanup.
- Upsert requires `key_columns`. Greenplum uses staged key delete-and-insert and
  does not require `upsert_partition_column`; when supplied, it augments the
  transactional delete-and-insert key.
- Trino and ClickHouse upsert require `upsert_partition_column`. They build a
  final increment for affected partitions, drop those partitions, and insert
  the final increment. Arbitrary key deletes and merge-style updates are avoided
  because they are slow, connector-dependent, or mutation-heavy. The complete
  preserved/incoming payload is certified before partition deletion and reused
  across transient retries; preservation is never recomputed from a partially
  replaced target. An exhausted retry retains the recovery stage names.
- Trino partition upsert also requires `upsert_partition_drop_sql_template` in
  the target connection config so connector-specific partition drop SQL is
  explicit. Actual string partition values are quoted as data, including values
  beginning with `<`; a raw synthetic marker is reserved for unknown partition
  values in offline plans.
- For Trino connections with the complete `s3_transfer_staging_schema` and
  `s3_transfer_staging_location` pair, `load_df` writes temporary Parquet files
  to the object-storage prefix, creates the external stage table in the S3
  staging schema, and finalizes into the target from that table. Python and
  Trino must both be able to access and clean up the prefix.
- If the S3 pair is not configured, Trino `load_df` is not Parquet-based and
  uses direct dataframe inserts controlled by `trino_insert_chunk_size`.
  `transfer_staging_schema` remains available for ordinary SQL staging.
- ClickHouse target creation follows the resolved connection creation policy;
  existing append/upsert targets retain their verified physical layout. Stage
  cleanup drops only resources acknowledged as created by the current run. A
  visible collision or uncertain CREATE result does not establish ownership.
- SQL stage identifiers use one maximum-63-byte naming policy on Greenplum,
  Trino, and ClickHouse. Backend-specific catalog/schema qualification and
  quoting are preserved.
- Greenplum validates `gp_partitions` on every call, but renders it only when
  creating or recreating the final target. Existing append, truncate, and
  upsert targets are not repartitioned implicitly.

[SQL functions index](index.md)
