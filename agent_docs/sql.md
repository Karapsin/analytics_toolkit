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

## SQL Integration Execution and Watch Policy

- Adding or changing an integration test requires manifest coverage and a fast
  non-integration regression test; it does not require running local SQL
  integration during a normal implementation task.
- Do not invoke `run_checks(area="sql", level="integration")` during normal
  implementation, documentation, commit, or push completion. Invoke it only
  when the user explicitly requests local integration validation or during
  release readiness.
- Normal completion requires focused checks, pre-commit checks, and the
  exact-pushed-SHA required-check watch. Advisory core/auth integration starts
  on push, but agents must not poll it, wait for it, or extend the turn for it.
  Report its status or URL only if already available from the required-check
  watch.
- Release readiness is the exception: it must complete the exhaustive `all`
  integration profile with both ClickHouse transports.

## SQL Explorer Visual Review

- Every production SQL Explorer change and every change to its visual harness
  or scene manifest requires a complete agent-reviewed macOS capture before
  `git_workflow` commit or push.
- Start with `visual_workflow(action="start")`, then capture with its returned
  review ID. Capture always makes a uniquely named clone of the pinned macOS
  OCI digest. It refuses any name collision and must never inspect, run, stop,
  or delete a pre-existing VM.
- The guest runs at 1280x800 without a Tart display window. Headless VNC captures
  the full framebuffer. The workflow shuts down and deletes only its run-owned
  clone after the scene set, including on failure.
- The manifest must cover every literal SQL Explorer widget ID. Each scene must
  publish valid geometry, all required controls must be visible, panes and
  status layers must not overlap, and scrollbars must remain on the right.
- Open every full-resolution PNG returned by visual status, normally in the
  reported batches, and call `visual_review(...)` for every scene. Use `pass`
  only when the UI is coherent, unclipped, and follows the dark square-edged
  "brutal, but polished" design. `product_defect` and
  `infrastructure_failure` require notes and block completion.
- `visual_workflow(action="complete")` writes a private receipt below
  `.rag_index/`. It hashes the full reviewable content rather than the current
  commit SHA, so the same receipt remains valid across the immediately
  following local commit but becomes stale after any content change.
- Reference screenshots are directional only; do not add or compare pixel
  baselines for this gate.

## SQL Layout Notes

- `connection/config.py`: finds `.connections`, parses it as JSON, normalizes aliases to lowercase, validates fields, and resolves alias to backend.
- `connection/get_sql_connection.py`: opens backend clients and handles optional CA certificate files and generated bundles from the `.connections` directory.
- `ddl/create_table.py`: infers dataframe column types, quotes identifiers per backend, and builds ClickHouse distributed DDL.
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
