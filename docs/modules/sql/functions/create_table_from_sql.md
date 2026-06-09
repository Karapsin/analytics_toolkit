[SQL functions index](index.md)

# create_table_from_sql

Create a target table from a source SQL query schema and insert the query result by default.

```python
create_table_from_sql(source_db: 'str', table_name: 'str', sql: 'str', *, table_db: 'str | None' = None, insert_data: 'bool' = True, drop_target_if_exists: 'bool' = False, gp_distributed_by_key: 'list[str] | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str' = 'ReplicatedMergeTree', ch_cluster: 'str' = '{cluster}', ch_sharding_key: 'str' = 'rand()', ch_only_shard: 'bool' = False, ch_retry_per_host_drops: 'bool' = True, ch_retry_per_host_drops_concurrency: 'int | None' = None, trino_insert_chunk_size: 'int | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None, table_schema: 'dict[str, str] | None' = None) -> 'int | None | SqlPlan | SqlOperationResult'
```

## Inputs

### General Inputs

- `source_db`: Connection key or alias used to inspect the source query.
- `table_db`: Connection key or alias used to create the target table; defaults to `source_db`.
- `table_name`: Target or source table name, depending on the helper.
- `sql`: Source SQL text used for table creation from query metadata.
- `insert_data`: Whether to insert the source query result after creating a table from SQL metadata; defaults to `True`.
- `drop_target_if_exists`: Whether to drop an existing target before creating it.
- `dry_run`: When `True`, return a plan without mutating the database.
- `return_sql`: When `True`, return a `SqlPlan` instead of mutating a database.
- `return_metadata`: When `True`, return `SqlOperationResult` instead of the historical bare value.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.
- `table_schema`: Explicit backend-native column type mapping for created tables.
- `partition_by`: Partitioning columns or expression for created tables, interpreted according to the target backend.
- `order_by`: Ordering or sorting columns or expression for created tables, interpreted according to the target backend.

### Backend-Specific Inputs

- `gp_distributed_by_key`: Greenplum distribution key columns for created target tables.
- `trino_insert_chunk_size`: Number of rows per Trino parameterized multi-row insert statement.
- `ch_engine`: ClickHouse engine to use for created local shard tables.
- `ch_cluster`: ClickHouse cluster name or macro for distributed/shard DDL; `None` skips cluster DDL where supported.
- `ch_sharding_key`: ClickHouse sharding expression for distributed table creation.
- `ch_only_shard`: For ClickHouse, create or mutate only the local table instead of a distributed/shard pair.
- `ch_retry_per_host_drops`: Whether ClickHouse replace/drop flows may retry direct local drops on affected hosts.
- `ch_retry_per_host_drops_concurrency`: Maximum concurrent ClickHouse per-host cleanup connections; `None` uses the helper default.

## Usage

```python
from analytics_toolkit import sql

plan = sql.create_table_from_sql(
    "trino",
    "sandbox.orders_daily",
    "select order_date, count(*) as orders from sandbox.orders group by order_date",
    dry_run=True,
)
print(sql.format_plan(plan))
```

## Notes

For ClickHouse targets, the helper creates the managed shard and distributed
table pair unless `ch_only_shard=True`. When `insert_data=False`, the target
table structure is created but the source query result is not inserted.

[SQL functions index](index.md)
