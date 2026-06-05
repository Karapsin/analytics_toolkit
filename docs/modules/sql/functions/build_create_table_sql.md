[SQL functions index](index.md)

# build_create_table_sql

Render the primary `CREATE TABLE` statement for a dataframe and backend.

```python
build_create_table_sql(connection_type: 'str', table_name: 'str', batch: 'pd.DataFrame', column_types: 'Mapping[str, str] | None' = None, gp_distributed_by_key: 'list[str] | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str' = 'ReplicatedMergeTree', ch_cluster: 'str' = '{cluster}', ch_sharding_key: 'str' = 'rand()', ch_distributed_table: 'bool' = False, ch_only_shard: 'bool' = False, ch_replace_table: 'bool' = False, query_label: 'str | None' = None, table_schema: 'Mapping[str, str] | None' = None) -> 'str'
```

## Inputs

### General Inputs

- `connection_type`: Connection key or alias from `.connections`; backend dispatch is selected from that entry.
- `table_name`: Target or source table name, depending on the helper.
- `batch`: Dataframe whose columns are used to infer table DDL.
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
ddl = sql.build_create_table_sql("gp", "sandbox.scores", batch)
print(ddl)
```

## Notes

- This renders SQL only and does not open a connection.

[SQL functions index](index.md)
