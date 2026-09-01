[SQL functions index](index.md)

# execute_create

Execute setup statements and create a table from the final `SELECT` using the
connection's regular DDL defaults. All statements use one connection.

```python
execute_create(db_key: 'str', table_name: 'str', query: 'str', *, drop_if_exists: 'bool' = False, if_not_exists: 'bool' = False, gp_distributed_by_key: 'str | Sequence[str] | None' = None, gp_partitions: 'Mapping[str, Any] | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str | None' = None, ch_cluster: 'str | None' = None, ch_sharding_key: 'str | None' = None, ch_distributed_table: 'bool | None' = None, ch_distributed_engine_template: 'str | None' = None, ch_distributed_cluster: 'str | None' = None, ch_shard_on_cluster: 'str | None' = None, ch_distributed_on_cluster: 'str | None' = None, ch_ddl_ready_timeout_seconds: 'float | None' = None, ch_ddl_wait_policy: 'str | None' = None, ch_only_shard: 'bool' = False, print_queries: 'bool' = False, gp_break_query: 'bool' = False, gp_commit_each_statement: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, progress: 'bool' = False, retry_policy: 'ExecuteRetryPolicy' = 'safe') -> 'int | SqlPlan | SqlOperationResult'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias used for every statement
- `table_name` - target table to create
- `query` - setup statements followed by one final `SELECT`
- `drop_if_exists` - whether to drop the target before setup and creation
- `if_not_exists` - whether to skip the whole operation when the target already exists
- `partition_by` - partitioning columns or expression passed to regular target DDL defaults
- `order_by` - ordering or sorting columns or expression passed to regular target DDL defaults
- `print_queries` - whether to print submitted SQL
- `retry_cnt` - maximum whole-operation attempts
- `timeout_increment` - delay increment between retries
- `query_label` - safe label added to SQL comments, plans, metadata, and logs
- `dry_run` - whether to return an ordered plan without executing SQL
- `return_sql` - whether to return the same ordered plan instead of executing SQL
- `return_metadata` - whether to wrap the affected-row count and plan in `SqlOperationResult`
- `progress` - whether to show multi-statement execution progress
- `retry_policy` - `safe`, `always`, or `never` mutation replay policy

### Backend-Specific Inputs

- `gp_distributed_by_key` - Greenplum distribution key column or columns
- `gp_partitions` - Greenplum initial range or list partition definitions
- `gp_break_query` - whether Greenplum should split and execute setup statements separately
- `gp_commit_each_statement` - whether Greenplum should commit each split setup statement
- `ch_engine` - ClickHouse shard-table engine override
- `ch_cluster` - deprecated ClickHouse cluster compatibility shortcut
- `ch_sharding_key` - ClickHouse distributed-table sharding expression
- `ch_distributed_table` - whether to create a ClickHouse shard/facade pair
- `ch_distributed_engine_template` - ClickHouse `Distributed(...)` engine template override
- `ch_distributed_cluster` - ClickHouse routing cluster inside the distributed engine
- `ch_shard_on_cluster` - ClickHouse execution cluster for shard DDL
- `ch_distributed_on_cluster` - ClickHouse execution cluster for facade DDL
- `ch_ddl_ready_timeout_seconds` - ClickHouse post-create readiness deadline
- `ch_ddl_wait_policy` - ClickHouse `wait_all`, `wait_shard`, `wait_distr`, or `wait_none` policy
- `ch_only_shard` - whether ClickHouse should create only the local shard table

## Usage

```python
from analytics_toolkit import sql

created = sql.execute_create(
    "gp",
    "mart.daily_scores",
    """
    CREATE TEMP TABLE current_scores AS SELECT * FROM raw.scores;
    SELECT user_id, score FROM current_scores
    """,
    drop_if_exists=True,
)

created
# 42
```

The default uses plain `CREATE TABLE` and fails if the target exists.
`drop_if_exists=True` replaces it. `if_not_exists=True` checks the target before
setup and skips the whole operation when it exists; the two flags are mutually
exclusive.

Greenplum partition options trigger schema inspection followed by a normal
partitioned create and positional insert. ClickHouse distributed-pair defaults
create the shard and facade with `EMPTY AS`, wait for DDL readiness, and then
insert exactly once through the facade.

[SQL functions index](index.md)
