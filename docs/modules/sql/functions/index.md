[SQL module index](../index.md)

# SQL Functions

Use `from analytics_toolkit import sql` or `import analytics_toolkit.sql as sql`
in user-facing code. Deep imports under `analytics_toolkit.sql.*` are internal
and may change.

General functions are listed before backend-specific functions. Within each
section, the helpers most likely to be used in normal workflows appear first.

## General Functions

- [read](read.md) - run one query and return a dataframe
- [execute](execute.md) - run SQL without returning a dataframe
- [execute_read](execute_read.md) - execute setup SQL, then read the final query
- [show_queries](show_queries.md) - list visible backend queries
- [cancel_queries](cancel_queries.md) - cancel running backend queries
- [load_df](load_df.md) - load a dataframe into a SQL table
- [transfer](transfer.md) - stream query results into another table
- [create_sql_table](create_sql_table.md) - create a table from schema, dataframe, or query
- [table_info](table_info.md) - inspect table existence and metadata
- [show_tables](show_tables.md) - list backend tables
- [drop_tables](drop_tables.md) - drop one or more tables
- [drop_partitions](drop_partitions.md) - drop table partition values
- [cleanup_stale_stage_tables](cleanup_stale_stage_tables.md) - drop stale transfer stage tables for a target table
- [extract_ddl](extract_ddl.md) - return native table DDL
- [format_plan](format_plan.md) - render a dry-run SQL plan
- [async_sql](async_sql.md) - run SQL task specs concurrently
- [parallel_sql](parallel_sql.md) - run independent SQL tasks in parallel
- [validate_connections](validate_connections.md) - validate configured connections
- [set_missing_env_variables](set_missing_env_variables.md) - prompt for unset environment references
- [generate_dummy_connections](generate_dummy_connections.md) - create starter connection config files

## Backend-Specific Functions

- [ch_reconfigure_table](ch_reconfigure_table.md) - reconfigure ClickHouse table storage and cluster attributes
- [gp_analyze_partitioned_table](gp_analyze_partitioned_table.md) - analyze Greenplum leaf partitions
- [gp_create_partitions](gp_create_partitions.md) - create Greenplum partitions
- [gp_vacuum](gp_vacuum.md) - run Greenplum vacuum

[SQL module index](../index.md)
