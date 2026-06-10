[SQL module index](../index.md)

# SQL Functions

Use `from analytics_toolkit import sql` or `import analytics_toolkit.sql as sql`
in user-facing code. Deep imports under `analytics_toolkit.sql.*` are internal
and may change.

General functions are listed before backend-specific functions. Within each
section, the helpers most likely to be used in normal workflows appear first.

## General Functions

- [read](read.md): Run one query and return a dataframe.
- [execute](execute.md): Run SQL without returning a dataframe.
- [execute_read](execute_read.md): Execute setup SQL, then read the final query.
- [cancel_queries](cancel_queries.md): Cancel running backend queries.
- [load_df](load_df.md): Load a dataframe into a SQL table.
- [transfer](transfer.md): Stream query results into another table.
- [create_sql_table](create_sql_table.md): Create a table from schema, dataframe, or query.
- [table_info](table_info.md): Inspect table existence and metadata.
- [show_tables](show_tables.md): List backend tables.
- [drop_tables](drop_tables.md): Drop one or more tables.
- [drop_paritions](drop_paritions.md): Drop table partition values.
- [extract_ddl](extract_ddl.md): Return native table DDL.
- [format_plan](format_plan.md): Render a dry-run SQL plan.
- [async_sql](async_sql.md): Run SQL task specs concurrently.
- [parallel_sql](parallel_sql.md): Run independent SQL tasks in parallel.
- [validate_connections](validate_connections.md): Validate configured connections.
- [generate_dummy_connections](generate_dummy_connections.md): Create starter connection config files.

## Backend-Specific Functions

- [gp_create_partitions](gp_create_partitions.md): Create Greenplum partitions.
- [gp_vacuum](gp_vacuum.md): Run Greenplum vacuum.

[SQL module index](../index.md)
