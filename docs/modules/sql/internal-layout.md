[SQL module index](index.md)

# Internal Layout

Supported user-facing import style is `from analytics_toolkit import sql` or
`import analytics_toolkit.sql as sql`. Deep imports under
`analytics_toolkit.sql.*` are internal only and may change; call public helpers
through the `sql` facade, for example `sql.create_sql_table(...)`,
`sql.load_df(...)`, or `sql.transfer(...)`. Do not restore removed root implementation paths.

- `core/`: backend capabilities, identifiers, and public type aliases
- `execution/`: timing, retry wrappers, plans, plan steps, labels, and query
  timing
- `orchestration/`: async and thread-based parallel task runners
- `metadata/`: table listing and table inspection helpers
- `connection/`: connection config and backend connection creation
- `ddl/`: table-creation and DDL extraction helpers
- `dml/io/`: read and execute operations
- `dml/load/`: dataframe loading and staging helpers
- `dml/table/`: table operations, validation, partition helpers, and
  ClickHouse table moves
- `dml/transfer/`: table transfer flow and runtime models
- `clickhouse/`: ClickHouse lifecycle, options, and wait helpers

[SQL module index](index.md)
