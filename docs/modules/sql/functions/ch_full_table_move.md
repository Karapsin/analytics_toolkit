[SQL functions index](index.md)

# ch_full_table_move

Move a ClickHouse distributed/shard table pair to a new name and drop the source pair.

```python
ch_full_table_move(db_key: 'str', move_table: 'str', to_table: 'str', *, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str | None' = None, ch_cluster: 'str | None' = '{cluster}', sharding_key: 'str | None' = None, ch_retry_per_host_drops: 'bool' = True, ch_retry_per_host_drops_concurrency: 'int | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None) -> 'SqlPlan | SqlOperationResult | None'
```

## Inputs

- `db_key`: Connection key or alias from `.connections`.
- `move_table`: Source ClickHouse table to move.
- `to_table`: Target table name.
- `partition_by`: Backend-specific partitioning columns or expression for created tables.
- `order_by`: Backend-specific ordering or sorting columns for created tables.
- `ch_engine`: ClickHouse engine to use for created local shard tables.
- `ch_cluster`: ClickHouse cluster name or macro for distributed/shard DDL; `None` skips cluster DDL where supported.
- `sharding_key`: ClickHouse sharding expression for distributed table creation.
- `ch_retry_per_host_drops`: Whether ClickHouse replace/drop flows may retry direct local drops on affected hosts.
- `ch_retry_per_host_drops_concurrency`: Maximum concurrent ClickHouse per-host cleanup connections; `None` uses the helper default.
- `dry_run`: When `True`, return a plan without mutating the database.
- `return_sql`: When `True`, return a `SqlPlan` instead of mutating a database.
- `return_metadata`: When `True`, return `SqlOperationResult` instead of the historical bare value.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.

## Usage

```python
from analytics_toolkit import sql

plan = sql.ch_full_table_move(
    "ch",
    move_table="sandbox.events_daily_old",
    to_table="sandbox.events_daily",
    dry_run=True,
)
print(sql.format_plan(plan))
```

## Notes

- ClickHouse-only helper.

[SQL functions index](index.md)
