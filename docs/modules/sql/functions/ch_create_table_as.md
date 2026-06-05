[SQL functions index](index.md)

# ch_create_table_as

Recreate a ClickHouse distributed/shard table pair from a query result.

```python
ch_create_table_as(db_key: 'str', table_name: 'str', query: 'str', *, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str' = 'ReplicatedMergeTree', ch_cluster: 'str' = '{cluster}', ch_sharding_key: 'str' = 'rand()', ch_only_shard: 'bool' = False, ch_retry_per_host_drops: 'bool' = True, ch_retry_per_host_drops_concurrency: 'int | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, query_label: 'str | None' = None, return_metadata: 'bool' = False, table_schema: 'dict[str, str] | None' = None) -> 'SqlPlan | SqlOperationResult | None'
```

## Inputs

### General Inputs

- `db_key`: Connection key or alias from `.connections`.
- `table_name`: Target or source table name, depending on the helper.
- `query`: SQL text to execute or read.
- `dry_run`: When `True`, return a plan without mutating the database.
- `return_sql`: When `True`, return a `SqlPlan` instead of mutating a database.
- `return_metadata`: When `True`, return `SqlOperationResult` instead of the historical bare value.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.
- `table_schema`: Explicit backend-native column type mapping for created tables.
- `partition_by`: Partitioning columns or expression for created tables, interpreted according to the target backend.
- `order_by`: Ordering or sorting columns or expression for created tables, interpreted according to the target backend.

### Backend-Specific Inputs

- `ch_engine`: ClickHouse engine to use for created local shard tables.
- `ch_cluster`: ClickHouse cluster name or macro for distributed/shard DDL; `None` skips cluster DDL where supported.
- `ch_sharding_key`: ClickHouse sharding expression for distributed table creation.
- `ch_only_shard`: For ClickHouse, create or mutate only the local table instead of a distributed/shard pair.
- `ch_retry_per_host_drops`: Whether ClickHouse replace/drop flows may retry direct local drops on affected hosts.
- `ch_retry_per_host_drops_concurrency`: Maximum concurrent ClickHouse per-host cleanup connections; `None` uses the helper default.

## Usage

```python
from analytics_toolkit import sql

plan = sql.ch_create_table_as(
    "ch",
    "sandbox.events_daily",
    "select event_date, count() as views from sandbox.events group by event_date",
    order_by=["event_date"],
    dry_run=True,
)
print(sql.format_plan(plan))
```

## Notes

- ClickHouse-only helper.

[SQL functions index](index.md)
