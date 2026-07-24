[SQL functions index](index.md)

# load_df

Load a pandas dataframe into a SQL table on a configured backend.

```python
load_df(db_key: 'str', destination_table: 'str', df: 'pd.DataFrame', append: 'bool' = False, write_mode: 'str | None' = None, gp_distributed_by_key: 'str | Sequence[str] | None' = None, gp_partitions: 'Mapping[str, Any] | None' = None, key_columns: 'str | Sequence[str] | None' = None, upsert_partition_column: 'str | None' = None, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, trino_insert_chunk_size: 'int | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str | None' = None, ch_cluster: 'str | None' = None, ch_sharding_key: 'str | None' = None, ch_distributed_table: 'bool | None' = None, ch_distributed_engine_template: 'str | None' = None, ch_distributed_cluster: 'str | None' = None, ch_shard_on_cluster: 'str | None' = None, ch_distributed_on_cluster: 'str | None' = None, ch_only_shard: 'bool' = False, ch_retry_per_host_drops: 'bool' = True, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None, gp_insert_chunk_size: 'int | None' = None, progress: 'bool' = False, table_schema: 'dict[str, str] | None' = None) -> 'int | SqlPlan | SqlOperationResult'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias from `.connections`; backend dispatch is selected from that entry
- `destination_table` - target table name for dataframe loading
- `df` - dataframe to load
- `append` - historical dataframe loading flag; `True` appends and `False` replaces unless `write_mode` is supplied
- `write_mode` - explicit write behavior: append, replace, truncate_insert, or upsert
- `key_columns` - key column or columns used to validate staged rows and required when `write_mode="upsert"`
- `upsert_partition_column` - single staged column that defines affected partitions for Trino and ClickHouse upsert replacement; required for those backends when `write_mode="upsert"`
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

- `write_mode` can make append, replace, or truncate_insert behavior explicit while preserving historical `append` defaults.
- `write_mode="upsert"` stages incoming rows and rejects duplicate staged keys
  before any target mutation.
- When an upsert target already exists, the existing target schema is used for
  final insert column types; otherwise the target is created from `table_schema`
  or inferred dataframe types.
- When `load_df` creates a target table that did not exist at the start and the
  load later fails, it drops that newly created target during cleanup.
- Upsert requires `key_columns`. Greenplum uses staged key delete-and-insert and
  does not require `upsert_partition_column`.
- Trino and ClickHouse upsert require `upsert_partition_column`. They build a
  final increment for affected partitions, drop those partitions, and insert
  the final increment. Arbitrary key deletes and merge-style updates are avoided
  because they are slow, connector-dependent, or mutation-heavy.
- Trino partition upsert also requires `upsert_partition_drop_sql_template` in
  the target connection config so connector-specific partition drop SQL is
  explicit.
- For Trino connections with both `transfer_staging_schema` and
  `transfer_staging_location` configured, `load_df` writes temporary Parquet
  files to the object-storage prefix, creates an external stage table in
  `transfer_parquet_staging_schema` when configured (otherwise
  `transfer_staging_schema`), and finalizes into the target table from that
  stage. Python and Trino must both be able to access and clean up that prefix.
- If `transfer_staging_location` is not configured, Trino `load_df` keeps using
  direct dataframe inserts controlled by `trino_insert_chunk_size`.
- ClickHouse targets create distributed/shard table pairs unless `ch_only_shard=True`.
- Greenplum validates `gp_partitions` on every call, but renders it only when
  creating or recreating the final target. Existing append, truncate, and
  upsert targets are not repartitioned implicitly.

[SQL functions index](index.md)
