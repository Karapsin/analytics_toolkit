# SQL Agent Instructions

Read this file for SQL module code, tests, docs, API explanation, or behavior
investigation.

## SQL Module Contracts

- Public SQL APIs accept configured DB keys from `.connections`; callers should not need to pass raw backend names or open connection objects.
- Single-DB public SQL functions should name that argument `db_key`.
- Multi-DB public SQL functions may use directional key names such as `from_db`, `to_db`, `source_db`, and `table_db`.
- User-facing SQL imports should use `from analytics_toolkit import sql` or `import analytics_toolkit.sql as sql`. Deep imports under `analytics_toolkit.sql.*` are internal and may change. Do not restore removed root implementation paths.
- Public SQL input names that work only for one backend must use the backend prefix: `gp_`, `trino_`, or `ch_`. Do not keep unprefixed compatibility aliases for those backend-only inputs unless the user explicitly asks for compatibility.
- Each `.connections` value must include `type` as `gp`, `trino`, or `ch`. Backend dispatch comes from this `type`, while reconnect/retry/log messages keep using the alias key.
- Env-based SQL config such as `SQL_CONNECTIONS`, `GP_HOST`, `TRINO_HOST`, `CH_HOST`, `TRINO_INSERT_CHUNK_SIZE`, and config-file override env vars is intentionally unsupported. Do not restore fallback support.
- Keep public directional names such as `from_db`, `to_db`, `source_db`, and `table_db` compatible even when they now represent aliases.
- A Trino target may define `insert_chunk_size` in its connection config. Explicit function arguments override config; config overrides the internal default.
- `sql.read`, `sql.execute`, `load_df`, and `sql.transfer` retry the whole public operation with fresh connections. Preserve Greenplum rollback behavior on errors.
- `transfer` and `load_df` separate key and backend in option models. Same-backend aliases are valid as long as the alias keys differ.
- ClickHouse load/transfer creates and manages a shard table plus a `Distributed` table. Preserve local and cluster DDL/drop/truncate behavior.
- Key validation uses normalized unique key lists and null-safe joins for staged-vs-target overlap checks.
- Trino table metadata helpers need the alias key so unqualified names can use that connection's catalog/schema.
- Integration Trino targets use the Iceberg catalog, while raw Parquet stages
  use the separate Hive catalog named by `transfer_staging_schema`. Do not point
  external Parquet stages at an Iceberg schema: Iceberg tracks data files in
  table metadata and does not expose Hive's `external_location` contract.
- Every integration resource name and query label must include the profile run
  ID and scenario test ID. Register resources before creation so partial
  failures remain cleanable.

## SQL Layout Notes

- `connection/config.py`: finds `.connections`, parses it as JSON, normalizes aliases to lowercase, validates fields, and resolves alias to backend.
- `connection/get_sql_connection.py`: opens backend clients and handles optional CA certificate files and generated bundles from the `.connections` directory.
- `ddl/create_sql_table.py`: infers dataframe column types, quotes identifiers per backend, and builds ClickHouse distributed DDL.
- `dml/io`: read/execute helpers using `sqlparse`; `read_sql` accepts exactly one statement.
- `dml/load`: dataframe loading, stage table creation, batch insertion, Trino chunking, and backend-specific scalar normalization.
- `dml/table`: shared table existence, analyze, drop, vacuum, stage finalization, and validation helpers.
- `dml/transfer`: staged transfer flow, source streaming, full retry/restart behavior, and connection replacement helpers.

## Backend Adapter Placement

- Put backend-specific SQL, system-table queries, backend state mapping, and
  backend-specific DDL/DML fragments in the backend adapter layer under
  `analytics_toolkit/sql/backends/` or a backend-local helper module imported by
  that adapter.
- Keep generic SQL helpers focused on orchestration, option normalization, retry
  flow, dataframe normalization, and public API shape. Do not add new
  `backend == ...` dispatch branches in generic modules unless there is already
  an explicit local allowance and no adapter method fits.
