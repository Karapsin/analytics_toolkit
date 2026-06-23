[SQL functions index](index.md)

# load_df

Load a pandas dataframe into a SQL table on a configured backend.

```python
load_df(db_key: 'str', destination_table: 'str', df: 'pd.DataFrame', append: 'bool' = False, write_mode: 'str | None' = None, gp_distributed_by_key: 'str | Sequence[str] | None' = None, key_columns: 'str | Sequence[str] | None' = None, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, trino_insert_chunk_size: 'int | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str' = 'ReplicatedMergeTree', ch_cluster: 'str' = '{cluster}', ch_sharding_key: 'str' = 'rand()', ch_only_shard: 'bool' = False, ch_retry_per_host_drops: 'bool' = True, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None, gp_insert_chunk_size: 'int | None' = None, progress: 'bool' = False, table_schema: 'dict[str, str] | None' = None) -> 'int | SqlPlan | SqlOperationResult'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias from `.connections`; backend dispatch is selected from that entry
- `destination_table` - target table name for dataframe loading
- `df` - dataframe to load
- `append` - historical dataframe loading flag; `True` appends and `False` replaces unless `write_mode` is supplied
- `write_mode` - explicit write behavior: append, replace, truncate_insert, or upsert
- `key_columns` - key column or columns used to validate staged rows and required when `write_mode="upsert"`
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
- `gp_insert_chunk_size` - dataframe insert page size for Greenplum
- `trino_insert_chunk_size` - number of rows per Trino parameterized multi-row insert statement
- `ch_engine` - engine to use for created ClickHouse local shard tables
- `ch_cluster` - cluster name or macro for ClickHouse distributed/shard DDL; `None` skips cluster DDL where supported
- `ch_sharding_key` - sharding expression for ClickHouse distributed table creation
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
- `write_mode="upsert"` stages incoming rows, rejects duplicate staged keys,
  and then replaces matching target keys before inserting staged rows.
- When an upsert target already exists, the existing target schema is used for
  final insert column types; otherwise the target is created from `table_schema`
  or inferred dataframe types.
- Upsert requires `key_columns`. Trino uses native `MERGE`; connector support
  for `MERGE` is checked by Trino at runtime. Greenplum uses staged
  delete-and-insert. ClickHouse uses lightweight `DELETE` plus insert and does
  not require `ReplacingMergeTree`.
- For Trino connections with both `transfer_staging_schema` and
  `transfer_staging_location` configured, `load_df` writes temporary Parquet
  files to the object-storage prefix, creates an external stage table in
  `transfer_staging_schema`, and finalizes into the target table from that
  stage. Python and Trino must both be able to access and clean up that prefix.
- If `transfer_staging_location` is not configured, Trino `load_df` keeps using
  direct dataframe inserts controlled by `trino_insert_chunk_size`.
- ClickHouse targets create distributed/shard table pairs unless `ch_only_shard=True`.

[SQL functions index](index.md)
