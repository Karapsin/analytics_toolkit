[SQL functions index](index.md)

# create_sql_table

Create a SQL table from dataframe column metadata and backend-specific options.

```python
create_sql_table(connection_type: 'str', connection: 'Any', table_name: 'str', batch: 'pd.DataFrame', column_types: 'Mapping[str, str] | None' = None, gp_distributed_by_key: 'list[str] | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str' = 'ReplicatedMergeTree', ch_cluster: 'str' = '{cluster}', ch_sharding_key: 'str' = 'rand()', ch_distributed_table: 'bool' = False, ch_only_shard: 'bool' = False, ch_replace_table: 'bool' = False, dry_run: 'bool' = False, return_sql: 'bool' = False, query_label: 'str | None' = None, return_metadata: 'bool' = False, table_schema: 'Mapping[str, str] | None' = None) -> 'SqlPlan | SqlOperationResult | None'
```

## Inputs

### General Inputs

- `connection_type`: Connection key or alias from `.connections`; backend dispatch is selected from that entry.
- `connection`: Open backend connection object used by the table creation helper.
- `table_name`: Target or source table name, depending on the helper.
- `batch`: Dataframe whose columns are used to infer table DDL.
- `dry_run`: When `True`, return a plan without mutating the database.
- `return_sql`: When `True`, return a `SqlPlan` instead of mutating a database.
- `return_metadata`: When `True`, return `SqlOperationResult` instead of the historical bare value.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.
- `table_schema`: Explicit backend-native column type mapping for created tables.
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

batch = pd.DataFrame({"user_id": [1], "score": [10.5]})
connection = sql.get_sql_connection("gp")
sql.create_sql_table("gp", connection, "sandbox.scores", batch)
```

## Notes

- This helper expects an already opened backend connection; most callers use `load_df` or `create_table_from_sql` instead.

[SQL functions index](index.md)
