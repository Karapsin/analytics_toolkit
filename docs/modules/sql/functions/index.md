[SQL module index](../index.md)

# SQL Functions

Use `from analytics_toolkit import sql` or `import analytics_toolkit.sql as sql`
in user-facing code. Deep imports under `analytics_toolkit.sql.*` are internal
and may change.

General functions are listed before backend-specific functions. Within each
section, the helpers most likely to be used in normal workflows appear first.

## General Functions

- [read](read.md)
- [execute](execute.md)
- [execute_read](execute_read.md)
- [load_df](load_df.md)
- [transfer](transfer.md)
- [create_table_from_sql](create_table_from_sql.md)
- [create_sql_table](create_sql_table.md)
- [build_create_table_sql](build_create_table_sql.md)
- [table_info](table_info.md)
- [show_tables](show_tables.md)
- [drop_many_partitions](drop_many_partitions.md)
- [extract_ddl](extract_ddl.md)
- [format_plan](format_plan.md)
- [async_sql](async_sql.md)
- [parallel_sql](parallel_sql.md)
- [validate_connections](validate_connections.md)
- [generate_dummy_connections](generate_dummy_connections.md)
- [get_sql_connection](get_sql_connection.md)
- [with_sql_connection](with_sql_connection.md)
- [airflow_connection_config](airflow_connection_config.md)
- [use_airflow_connections](use_airflow_connections.md)
- [format_support_matrix](format_support_matrix.md)
- [support_matrix_rows](support_matrix_rows.md)

## Backend-Specific Functions

- [gp_create_many_partitions](gp_create_many_partitions.md)
- [build_gp_create_many_partitions_sqls](build_gp_create_many_partitions_sqls.md)
- [gp_vacuum](gp_vacuum.md)
- [gp_cancel_all_running_queries](gp_cancel_all_running_queries.md)
- [ch_create_table_as](ch_create_table_as.md)
- [ch_drop_table](ch_drop_table.md)
- [ch_full_table_move](ch_full_table_move.md)

[SQL module index](../index.md)
