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
caller session, and issues cancellation plus termination with optional
concurrency. For Greenplum, pass backend PIDs from `pg_stat_activity` as query
ids; `sql.cancel_queries` runs `pg_cancel_backend(pid)` followed by
`pg_terminate_backend(pid)` for each target PID. Use `sql.cancel_queries` for
operational cleanup, not as normal flow control.

## Table Defaults

Created Greenplum tables default to append-only, column-oriented storage with
compression. Distribution is random unless a distribution key is provided.

Partitioned parent tables are created with one `partition_by` column and an
inline `gp_partitions` range or list mapping. The mapping creates the initial
children in the same `CREATE TABLE`, with no default partition.
These defaults apply to table creation and write workflows such as
[sql.create_sql_table](functions/create_sql_table.md),
[sql.load_df](functions/load_df.md), and [sql.transfer](functions/transfer.md).
The mapping is creation-only: existing append, truncate, or upsert targets are
never altered implicitly. Use
[sql.gp_create_partitions](functions/gp_create_partitions.md) to add later,
non-overlapping Greenplum child partitions.

[SQL module index](index.md)
