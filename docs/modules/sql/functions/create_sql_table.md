[SQL functions index](index.md)

# create_sql_table

Create a SQL table from dataframe column metadata or an explicit schema.

```python
create_sql_table(db_key: 'str', table_name: 'str', df: 'pd.DataFrame | None' = None, *, column_types: 'Mapping[str, str] | None' = None, gp_distributed_by_key: 'list[str] | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str' = 'ReplicatedMergeTree', ch_cluster: 'str' = '{cluster}', ch_sharding_key: 'str' = 'rand()', ch_distributed_table: 'bool' = False, ch_only_shard: 'bool' = False, ch_replace_table: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, dry_run: 'bool' = False, return_sql: 'bool' = False, query_label: 'str | None' = None, return_metadata: 'bool' = False, table_schema: 'Mapping[str, str] | None' = None) -> 'SqlPlan | SqlOperationResult | None'
```

## Inputs

### General Inputs

- `db_key`: Connection key or alias from `.connections`; backend dispatch is selected from that entry.
- `table_name`: Target or source table name, depending on the helper.
- `df`: Optional dataframe whose columns are used to infer table DDL.
- `table_schema`: Explicit backend-native column type mapping for created tables; required when `df` is omitted.
- `retry_cnt`: Number of operation retries with fresh connections.
- `timeout_increment`: Delay increment used between operation retries.
- `dry_run`: When `True`, return a plan without mutating the database.
- `return_sql`: When `True`, return a `SqlPlan` instead of mutating a database.
- `return_metadata`: When `True`, return `SqlOperationResult` instead of the historical bare value.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.
- `partition_by`: Partitioning columns or expression for created tables, interpreted according to the target backend.
- `order_by`: Ordering or sorting columns or expression for created tables, interpreted according to the target backend.
- `column_types`: Optional backend-native column type mapping used by table DDL builders.

### Backend-Specific Inputs

- `gp_distributed_by_key`: Greenplum distribution key columns for created target tables.
- `ch_engine`: ClickHouse engine to use for created local shard tables.
- `ch_cluster`: ClickHouse cluster name or macro for distributed/shard DDL; `None` skips cluster DDL where supported.
- `ch_sharding_key`: ClickHouse sharding expression used for generated distributed table DDL.
- `ch_distributed_table`: Whether the rendered ClickHouse DDL should include a `Distributed` table.
- `ch_only_shard`: For ClickHouse, create or mutate only the local table instead of a distributed/shard pair.
- `ch_replace_table`: Whether rendered ClickHouse DDL should use replace-style table creation.

## Usage

```python
import pandas as pd
from analytics_toolkit import sql

df = pd.DataFrame({"user_id": [1], "score": [10.5]})
sql.create_sql_table(
    db_key="gp",
    table_name="sandbox.scores",
    df=df,
)
```

```python
from analytics_toolkit import sql

sql.create_sql_table(
    db_key="gp",
    table_name="sandbox.scores",
    table_schema={"user_id": "BIGINT", "score": "DOUBLE PRECISION"},
)
```

## Notes

- The public helper opens and closes its own connection and retries the whole create operation.

[SQL functions index](index.md)
