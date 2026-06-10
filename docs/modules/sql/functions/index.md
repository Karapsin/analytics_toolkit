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
- [create_sql_table](create_sql_table.md)
- [table_info](table_info.md)
- [show_tables](show_tables.md)
- [drop_tables](drop_tables.md)
- [drop_paritions](drop_paritions.md)
- [extract_ddl](extract_ddl.md)
- [format_plan](format_plan.md)
- [async_sql](async_sql.md)
- [parallel_sql](parallel_sql.md)
- [validate_connections](validate_connections.md)
- [generate_dummy_connections](generate_dummy_connections.md)

## Backend-Specific Functions

- [gp_create_many_partitions](gp_create_many_partitions.md)
- [gp_vacuum](gp_vacuum.md)
- [gp_cancel_all_running_queries](gp_cancel_all_running_queries.md)

[SQL module index](../index.md)
