[SQL module index](index.md)

# Greenplum Operations

Greenplum-specific operations cover maintenance and table-shape defaults that
are not portable to every backend. The maintenance helpers are
[sql.gp_vacuum](functions/gp_vacuum.md). Query cancellation is available through
the cross-backend [sql.cancel_queries](functions/cancel_queries.md) helper.

## Maintenance

Vacuum operations must run outside a transaction block. Use them for explicit
maintenance jobs where the caller controls when table cleanup or analyze work
happens.

Current-user query cancellation reads active backend sessions, excludes the
caller session, and issues cancellations with optional concurrency. For
Greenplum, pass backend PIDs from `pg_stat_activity` as query ids. Use
`sql.cancel_queries` for operational cleanup, not as normal flow control.

## Table Defaults

Created Greenplum tables default to append-only, column-oriented storage with
compression. Distribution is random unless a distribution key is provided.

Partitioned parent tables can be created with range partitioning options, while
child partitions are managed separately through the partitioning workflow.
These defaults apply to table creation and write workflows such as
[sql.create_sql_table](functions/create_sql_table.md),
[sql.load_df](functions/load_df.md), and [sql.transfer](functions/transfer.md).
Use [sql.gp_create_partitions](functions/gp_create_partitions.md) for
Greenplum child partitions.

[SQL module index](index.md)
