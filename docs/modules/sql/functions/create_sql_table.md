[SQL functions index](index.md)

# create_sql_table

Create a SQL table from exactly one schema source: a dataframe, a source SQL
query, or an explicit table schema.

```python
create_sql_table(db_key: 'str', table_name: 'str', df: 'pd.DataFrame | None' = None, *, sql: 'str | None' = None, source_db: 'str | None' = None, insert_data: 'bool' = False, drop_target_if_exists: 'bool' = False, gp_distributed_by_key: 'str | Sequence[str] | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str' = 'ReplicatedMergeTree', ch_cluster: 'str' = '{cluster}', ch_sharding_key: 'str' = 'rand()', ch_distributed_table: 'bool' = False, ch_only_shard: 'bool' = False, ch_replace_table: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, dry_run: 'bool' = False, return_sql: 'bool' = False, only_generate_sql: 'bool' = False, query_label: 'str | None' = None, return_metadata: 'bool' = False, table_schema: 'Mapping[str, str] | None' = None) -> 'str | int | SqlPlan | SqlOperationResult | None'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias from `.connections`; backend dispatch is selected from that entry
- `table_name` - target or source table name, depending on the helper
- `df` - dataframe whose columns are used to infer table DDL
- `sql` - source SQL query whose metadata defines the target columns
- `table_schema` - explicit backend-native column type mapping for created tables
- `source_db` - source connection key for `sql`; defaults to `db_key`
- `insert_data` - when `sql` is provided, also insert the query result after creating the table
- `drop_target_if_exists` - drop an existing target before SQL-source creation
- `retry_cnt` - number of operation retries with fresh connections
- `timeout_increment` - delay increment used between operation retries
- `dry_run` - when `True`, return a plan without mutating the database
- `return_sql` - when `True`, return a `SqlPlan` instead of mutating a database
- `only_generate_sql` - when `True`, return generated SQL as a formatted string instead of a plan or mutation
- `return_metadata` - when `True`, return `SqlOperationResult` instead of the historical bare value
- `query_label` - safe label added to generated SQL comments, plans, metadata, and logs
- `partition_by` - partitioning columns or expression for created tables, interpreted according to the target backend
- `order_by` - ordering or sorting columns or expression for created tables, interpreted according to the target backend

### Backend-Specific Inputs

- `gp_distributed_by_key` - distribution key column or columns for created Greenplum target tables
- `ch_engine` - engine to use for created ClickHouse local shard tables
- `ch_cluster` - cluster name or macro for ClickHouse distributed/shard DDL; `None` skips cluster DDL where supported
- `ch_sharding_key` - sharding expression used for generated ClickHouse distributed table DDL
- `ch_distributed_table` - whether the rendered ClickHouse DDL should include a `Distributed` table
- `ch_only_shard` - for ClickHouse, create or mutate only the local table instead of a distributed/shard pair
- `ch_replace_table` - whether rendered ClickHouse DDL should use replace-style table creation

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

```python
from analytics_toolkit import sql

ddl = sql.create_sql_table(
    db_key="gp",
    table_name="sandbox.scores",
    table_schema={"user_id": "BIGINT", "score": "DOUBLE PRECISION"},
    only_generate_sql=True,
)
```

```python
from analytics_toolkit import sql

sql.create_sql_table(
    db_key="gp",
    table_name="sandbox.scores",
    sql="select user_id, score from sandbox.source_scores",
    source_db="trino",
)
```

Output example:

```python
ddl
# 'CREATE TABLE sandbox.scores (...);'
```

## Notes

- The public helper opens and closes its own connections and retries the whole
  create operation. For SQL schema sources, each attempt starts again with
  source schema inspection, then target creation and the optional insert.
- Cross-backend SQL inserts use a single inner transfer attempt so
  `retry_cnt` controls the total number of whole-operation attempts rather than
  multiplying nested retry counts.
- A failed attempt removes a target created by that attempt before retrying. If
  cleanup cannot be confirmed, the operation stops instead of retrying against
  a partial target; pre-existing targets are never removed unless
  `drop_target_if_exists=True`.
- Pass exactly one of `df`, `sql`, or `table_schema`.
- `only_generate_sql=True` with `sql` inspects source query metadata but does not create, drop, or insert data.
- `retry_cnt` must be a built-in positive integer. `timeout_increment` must be
  a finite non-negative real number; the same validation applies to dry runs
  and generated-SQL paths.

[SQL functions index](index.md)
