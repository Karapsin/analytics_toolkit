[Documentation overview](README.md)

# Changelog

Generated from package version bumps and recent commit history.

## 1.3.8.14 - 2026-06-10

- Added `sql.cleanup_stale_stage_tables()`, a staged-table cleanup helper that
  discovers or accepts explicit stage tables and drops them with retry semantics.
- Made transfer staging cleanup always run when `transfer_staging_schema` is
  configured on the target connection.
- Removed `clean_transfer_staging_schema` from `sql.transfer` options so cleanup is
  always enabled by connection configuration.

## 1.3.8.12 - 2026-06-10

- Extracted staging-table cleanup into shared helpers used by DataFrame load and
  transfer staged flows.

## 1.3.8.11 - 2026-06-10

- Removed the `replace_target_table` argument from `sql.transfer`; write behavior now uses `write_mode` exclusively.
- Updated transfer docs and delegation paths to remove legacy compatibility input usage for transfer replacements.

## 1.3.8.10 - 2026-06-10

- Added `transfer_staging_schema` support to connection configuration for all SQL
  backends so transfers can create staging tables in a backend-specific schema.
- Changed transfer staging names to include the target user marker with
  `__analytics_toolkit_<user>__stage__<suffix>` when a transfer staging schema
  is configured.
- Added transfer schema cleanup lifecycle hooks that drop matching staging tables for
  the transfer user before transfer initialization and after finalization, while
  warning once when cleanup is enabled but staging schema is not configured.

## 1.3.8.9 - 2026-06-10

- Added ordered `time_print` context extensibility with a first-class `task_id`
  context field and wired `task_id` propagation through top-level `async_sql` and
  `parallel_sql` task dispatch paths.

## 1.3.8.8 - 2026-06-10

- Fixed throughput smoothing regression so windowed row/sec averaging is now
  compared against the previous smoothed throughput before applying deadband.
- Added per-batch transfer logging for batch duration, rows/second, and cumulative
  rows to the staged transfer progress output.

## 1.3.8.7 - 2026-06-10

- Added smoothed adaptive throughput control for transfer batching:
  `target_rows_per_second_window` and `target_rows_per_second_deadband`.
  Throughput adaptation now uses an average over the most recent batches with
  configurable deadband before growing/shrinking batch size.

## 1.3.8.6 - 2026-06-10

- Added optional transfer constraints `min_batch_seconds`, `max_batch_seconds`,
  `min_batch_memory_mb`, and `max_batch_memory_mb` for adaptive target tuning.
- Constrained adaptive time/memory targets are now propagated into runtime
  transfer batch sizing decisions and dry-run option output.

## 1.3.8.5 - 2026-06-10

- Added single explicit transfer adaptation target validation.
- `target_batch_seconds` and `target_batch_memory_mb` now conflict with
  `target_rows_per_second` so at most one mode can be configured explicitly.

## 1.3.8.4 - 2026-06-10

- Added throughput-first transfer batch adaptation via
  `target_rows_per_second=True` and switched it on by default.
- Kept `target_batch_seconds` as the fallback adaptive control for users that
  explicitly disable throughput optimization.

## 1.3.8.3 - 2026-06-10

- Corrected the public SQL partition removal helper name to
  `sql.drop_partitions()`.
- Removed the misspelled `sql.drop_paritions()` public export.

## 1.3.8.2 - 2026-06-10

- Removed the public ClickHouse per-host drop concurrency SQL input from load,
  transfer, and drop flows.
- Kept ClickHouse per-host drop retry controlled by `ch_retry_per_host_drops`,
  using the internal default worker count.

## 1.3.8.1 - 2026-06-10

- Renamed the public Greenplum partition creation helper from
  `sql.gp_create_many_partitions()` to `sql.gp_create_partitions()`.
- Removed the old `gp_create_many_partitions` facade export and function
  documentation page.

## 1.3.8.0 - 2026-06-10

- Replaced the public Greenplum-only `sql.gp_cancel_all_running_queries()`
  helper with cross-backend `sql.cancel_queries()`.
- Added explicit query id and current-user `cancel_all=True` cancellation for
  Greenplum, Trino, and ClickHouse.
- Rejected `trino_partition_column` on non-Trino partition drops before opening
  a database connection.

## 1.3.7.19 - 2026-06-10

- Replaced the public ClickHouse-only `sql.ch_drop_table()` helper with
  `sql.drop_tables()`.
- Added list input support, ClickHouse shard/distributed drop flags, and an
  `if_exists` switch to `sql.drop_tables()`.
- Renamed the public partition helper to `sql.drop_partitions()`.

## 1.3.7.18 - 2026-06-10

- Removed `sql.format_support_matrix`, `sql.support_matrix_rows`,
  `sql.airflow_connection_config`, `sql.use_airflow_connections`, and
  `sql.with_sql_connection` from the public SQL facade API.
- Kept `get_sql_connection` internal for SQL execution/load/transfer call paths.
- Removed facade-level function documentation pages for those non-public helpers and
  updated SQL support matrix/configuration docs to avoid facade references.

## 1.3.7.17 - 2026-06-09

- Merged SQL table creation into `sql.create_sql_table()` with dataframe,
  source SQL, or manual `table_schema` schema sources and `only_generate_sql`.
- Removed public `sql.build_create_table_sql()`, `sql.create_table_from_sql()`,
  `sql.ch_full_table_move()`, and `sql.build_gp_create_many_partitions_sqls()`.
- Added `only_generate_sql` to `sql.gp_create_many_partitions()`.

## 1.3.7.16 - 2026-06-09

- Removed public RAG package extras and `analytics-toolkit docs` CLI commands.
- Replaced public docs retrieval with stdlib-only, agent-only repository tooling
  under `agent_tools/`.

## 1.3.7.15 - 2026-06-09

- Changed `sql.create_table_from_sql()` to insert data by default, reuse the
  internal ClickHouse CTAS flow for same-alias ClickHouse queries, and keep
  ClickHouse schema-only creation available with `insert_data=False`.
- Removed `sql.ch_create_table_as()` from the public SQL facade and function
  documentation.

## 1.3.7.6 - 2026-06-08

- Expanded the PyPI README with concise SQL transfer, AB metric, and date
  helper examples linked to module and function documentation.

## 1.3.7.5 - 2026-06-06

- Moved release validation Python logic into organized helper modules and kept
  only the pre-commit and full PyPI release scripts at the top level.

## 1.3.7.4 - 2026-06-06

- Added release checks for module function documentation coverage,
  documentation navigation links, and README dependency metadata.

## 1.3.7.3 - 2026-06-06

- Added release checks that keep the README package version synchronized with
  `pyproject.toml`.
- Added retry handling for PyPI and TestPyPI artifact verification installs.

## 1.3.6.18 - 2026-06-05

- Reworked non-SQL module documentation into SQL-style function references and
  workflow guides.
- Removed the legacy analytics toolkit manual in favor of module documentation.
- Updated the package summary wording for the PyPI README.

## 1.3.6.17 - 2026-06-05

- Renamed backend-only SQL inputs to backend-prefixed names and updated SQL
  function documentation grouping rules.

## 1.3.6.16 - 2026-06-05

- Added `sql.generate_dummy_connections()` for writing starter direct or
  Airflow-source `.connections` files without overwriting existing files.

## 1.3.6.15 - 2026-06-05

- Changed Excel `prettify=True` percentage and bounded decimal display formats
  to two decimal places.

## 1.3.6.14 - 2026-06-05

- Reworked the PyPI README into a CRAN-style package summary.
- Added a concise root README installation section.
- Moved detailed documentation into `docs/` and linked it from the package
  summary.
- Added PyPI/TestPyPI documentation rules for docs-only changes and dependency
  table updates.

## 1.3.6.12 - 2026-06-04

- Added MIT license metadata for PyPI packaging.
- Added a TestPyPI trusted-publishing path to the publish workflow.
- Documented the PyPI/TestPyPI release checklist and trusted publisher setup.

## 1.3.6.1 - 2026-06-03

- Added regression coverage that keeps new `time_print` keyword-only options
  optional for SQL timing wrappers and public dry-run paths.

## 1.3.6.0 - 2026-06-03

- Extended `time_print` with level filtering, structured context, stream
  routing, scoped context, and injectable clocks while keeping the timestamp-only
  output format.
- Added public `time_print` configuration helpers through
  `analytics_toolkit.general`.
- Added structured context to central SQL operation/timing logs and warning
  levels for retry failure logs.

## 1.3.5.0 - 2026-06-03

- Added `compute_mde_only` for pre-test AB planning from historical variance.
- Added public AB option bundles `RatioMetricSpec` and `MdePlanningOptions`.
- Added quarter support to date helpers, including `add_quarters` and quarterly
  `gen_dates_list` sequences.
- Exported public SQL type aliases for common connection, backend, table, and
  task annotations.
- Added cross-module SQL to AB metrics to Excel report example.
- Added regression coverage for date sequence properties, formatter row
  ordering, SQL identifier quoting, SQL dry-run option coverage, and AB planning.

## Recent Pre-Changelog History

- `1.3.4.0`: added a guaranteed `group_size` row to AB metric formatting.
- `1.3.3.0`: added CUPED MDE outputs to AB metrics.
- `1.3.2.x`: treated deterministic ClickHouse and SQL semantic errors as
  non-retryable.
- `1.3.1.x`: added `sanitize_date` and strengthened CUPED tests.

[Documentation overview](README.md)
