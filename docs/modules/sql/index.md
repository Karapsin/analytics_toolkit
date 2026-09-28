[All module docs](../README.md)

# analytics_toolkit.sql

SQL utilities for reading, executing, loading, and transferring data through
configured Greenplum, Trino, and ClickHouse connections.

SQL text formatting and CTE rewrites live in
[analytics_toolkit.sql_format](../sql_format/index.md). The `sql` module stays
focused on configured database operations.

Dataframes returned by `sql.read` and `sql.execute_read` infer modern pandas
nullable dtypes from raw backend values. Nullable integers remain exact instead
of being coerced through floating point when a result also contains `NULL`.

ClickHouse aliases with `cluster_routing` automatically use a fully deployed
managed physical shard for named reads and writes, with a safe local
Distributed-facade fallback when full routing-cluster coverage cannot be
verified.

These pages explain concepts and workflows. Use the
[All SQL functions](functions/index.md) reference when you need exact signatures
and input defaults.

## All SQL Functions

- [All SQL functions](functions/index.md)

[execute_insert](functions/execute_insert.md), [insert](functions/insert.md),
[execute_create](functions/execute_create.md), and
[execute_read](functions/execute_read.md) accept independent query lists with
`concurrency`, `soft_concurrency_cap`, and `hard_concurrency_cap`, like
[execute](functions/execute.md). Results keep input order; each item uses a
separate connection while its statements remain sequential.

## Workflow Guides

- [Configuration](configuration.md)
- [DataFrame loading](dataframe-loading.md)
- [Transfers](transfers.md)
- [Write safety](write-safety.md)
- [Logging and observability](logging-and-observability.md)
- [Table creation](table-creation.md)
- [Metadata and DDL](metadata-and-ddl.md)
- [Parallel workflows](parallel-workflows.md)
- [Partitioning](partitioning.md)
- [ClickHouse distributed tables](clickhouse-distributed-tables.md)
- [Greenplum operations](greenplum-operations.md)
- [Backend support matrix](support-matrix.md)

[All module docs](../README.md)
