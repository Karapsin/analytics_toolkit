[Documentation overview](README.md)

# Changelog

Generated from package version bumps and recent commit history.

## Unreleased

- Tighten SQL backend registry guardrails.
- Continue SQL backend registry cleanup.
- Moved remaining SQL backend helper ownership into adapters.
- Fixed Greenplum transfer stage insert retries to refresh closed target connections.
- Fix SQL backend adapter autoreload compatibility.
- Use cancel and terminate for Greenplum query cancellation.
- Accept scalar string SQL key columns.
- Use fresh target connections for SQL transfer and load_df target actions.

## 1.3.10.9 - 2026-06-19

- Add Trino transfer mode option.
- Add workflow status diff summaries.
- Require inline predicate placeholders for keyed SQL transfer.
- Add from_table source input to SQL transfer.
- Add public SQL facade dry-run smoke tests.
- Harden SQL transfer imports against autoreload drift.
- Use per-worker stage tables for keyed SQL transfer concurrency.
- Add SQL-native AB metric comparison.
- Add three-way AB metric parity coverage.
- Refactor SQL backend registry.

## 1.3.10.8 - 2026-06-18

- Updated startup and version bump workflow.
- Route normal agent work through dev branch.
- Verify dev branch after startup.
- Push dev after agent commits.
- Consolidated AB metric batch APIs.
- Add Parquet object-storage staging for Trino transfers.
- Add Parquet object-storage staging for Trino load_df and make Parquet dependencies mandatory.
- Added configurable CTE spacing for sql_format output.
- Added union spacing and compact star select formatting.
- Add keyed SQL transfer slices.

## 1.3.10.7 - 2026-06-17

- Added three-way MDE parity coverage.

## 1.3.10.6 - 2026-06-17

- Added SQL-native MDE planning.

## 1.3.10.5 - 2026-06-16

- Refactor compute_mde_from_sql concurrency to load SQL windows via parallel_sql before in-memory scenario computation.

## 1.3.10.4 - 2026-06-16

- Added non_zero_truncate AB outlier policy as the default for sparse metrics.

## 1.3.10.3 - 2026-06-16

- Add compute_mde_from_sql concurrency.

## 1.3.10.2 - 2026-06-16

- Add SQL-backed MDE planning and start_dt window selection.

## 1.3.10.1 - 2026-06-16

- Add compute_mde aggregation policies.

## 1.3.10.0 - 2026-06-16

- Fixed agent tool root handling, fingerprint verification, and RAG ranking.

## 1.3.9.19 - 2026-06-16

- Hardened agent tool workflow safety.

## 1.3.9.18 - 2026-06-16

- Tightened agent MCP workflow guardrails.

## 1.3.9.17 - 2026-06-16

- Made agent tool commit staging explicit and improved routing status.

## 1.3.9.16 - 2026-06-16

- Harden agent workflow safety.

## 1.3.9.15 - 2026-06-16

- Hardened agent tool workflow safety.

## 1.3.9.14 - 2026-06-16

- Consolidated the agent MCP surface into intent-based workflow tools for
  startup, docs retrieval, status, version bumps, checks, git, and release
  actions.
- Updated agent instructions and agent-tools documentation to route normal
  agent work through MCP instead of direct docs assistant commands.
- Added regression coverage for the consolidated MCP CLI, status, checks, git,
  release, and version workflows.

## 1.3.9.13 - 2026-06-16

- Added mandatory agent-only MCP startup tooling for coding agents.
- Exposed docs RAG, instruction routing, repo health, version/changelog, and
  focused test recommendation helpers through `agent_tools/mcp_server.py`.
- Added `agent_tools/mcp_tool.sh` for readable terminal access to the same
  agent MCP helper functions.
- Documented the required MCP workflow in `AGENTS.md`.

## 1.3.9.12 - 2026-06-16

- Extended the agent-only docs RAG index to include `agent_docs/` and
  `agent_tools/README.md`.
- Added source metadata, query expansion, source-aware ranking, and stale-index
  warnings to `agent_tools/docs_assistant.py`.
- Added regression coverage for agent RAG retrieval while keeping public docs
  retrieval stable.

## 1.3.9.11 - 2026-06-15

- Added `trino_catalog` to `sql.show_tables()` so Trino table listings can
  use an explicit catalog when the connection alias does not configure one.

## 1.3.9.10 - 2026-06-15

- Added CUPED MDE planning columns to `compute_mde` using adjacent
  pre-experiment and experiment-like historical windows.
- Added `pre_exp_days` to configure the CUPED covariate window length.

## 1.3.9.9 - 2026-06-15

- Added a default `user_id="user_id"` argument to `compute_mde`.

## 1.3.9.8 - 2026-06-15

- Renamed the MDE planner public API to `compute_mde` and removed the old export.
- Changed `compute_mde` to build MDE planning grids across historical date
  windows and total planned experiment user counts.
- Added `compute_mde` validation for user-day grain, date windows, control share
  splits, and explicit-list or min/max/step scenario inputs.
- Changed AB outlier validation to accept `outliers_quantile=1` for keeping the
  maximum observed value unmodified.

## 1.3.9.7 - 2026-06-15

- Changed `compute_mde` to require direct `user_id`, `n0`, and `n1`
  inputs instead of the `MdePlanningOptions` bundle.
- Added `compute_mde` validation for missing, null, and duplicate user ids
  and conflicting mean and ratio metric names.
- Fixed the `compute_mde` documentation examples to show the actual output
  column names.

## 1.3.9.6 - 2026-06-15

- Made upsert finalization use explicit stage-to-target column lists for
  Greenplum and ClickHouse instead of positional `SELECT *`.
- Changed `sql.load_df(..., write_mode="upsert")` to use existing target column
  types when the target table already exists.
- Improved `sql.transfer(..., write_mode="upsert", dry_run=True)` so dry-run
  plans infer simple source query columns or show an explicit source-column
  placeholder when inference is not possible.

## 1.3.9.5 - 2026-06-15

- Added `write_mode="upsert"` for `sql.load_df()` and `sql.transfer()` with
  required `key_columns`, duplicate staged-key validation, Trino `MERGE`,
  Greenplum delete-and-insert finalization, and ClickHouse lightweight
  delete-and-insert finalization.

## 1.3.9.4 - 2026-06-15

- Added `adaptive_batch_size_step` to `sql.transfer` and changed
  rows-per-second adaptation to probe smaller and larger batch sizes before
  accepting or rolling back transfer and Greenplum insert page sizes.

## 1.3.9.3 - 2026-06-15

- Made Greenplum `sql.transfer` stage insert page size adapt from the
  `gp_insert_chunk_size` initial value when adaptive batching is enabled.

## 1.3.9.2 - 2026-06-12

- Changed `analytics_toolkit.sql_format` join conditions so `ON` starts two
  spaces after the `JOIN` indentation and join `AND` lines place `nd` directly
  under `ON`.

## 1.3.9.1 - 2026-06-12

- Changed `analytics_toolkit.sql_format` join rendering so join `ON` and `AND`
  lines align with `JOIN` lines, and removed the extra spacer between generated
  Greenplum temp-table `DROP` and `CREATE` statements.

## 1.3.9.0 - 2026-06-12

- Added `group_by_format` and `order_by_format` controls to
  `analytics_toolkit.sql_format` renderers.
- Changed the SQL formatter defaults to compact SELECT-list ordinals for
  eligible `GROUP BY` and `ORDER BY` clauses while preserving expression-based
  output through explicit `"expressions"` modes.

## 1.3.8.19 - 2026-06-12

- Changed `analytics_toolkit.sql_format` defaults to lowercase SQL keywords
  while keeping uppercase available through `keyword_case="upper"`.
- Added `sql_format.gp_rewrite_to_temp_tables()` for Greenplum temp-table
  materialization scripts from SELECT CTEs and subqueries.

## 1.3.8.18 - 2026-06-12

- Changed `sql_format.format_sql` WHERE anchor formatting so `WHERE 1=1`
  stays on one line and following `AND` conditions align below the anchor.

## 1.3.8.17 - 2026-06-12

- Added `analytics_toolkit.sql_format` with deterministic SQL formatting and
  conservative derived SELECT subquery rewrites into CTEs.
- Added SQL formatting module documentation and focused tests for formatting,
  dialect parsing, WHERE anchors, and CTE rewrite failures.

## 1.3.8.16 - 2026-06-11

- Added `gp_insert_chunk_size` to `sql.transfer` so Greenplum stage inserts can
  tune `execute_values` page sizes.

## 1.3.8.15 - 2026-06-11

- Fixed Greenplum transfer staging names so generated table identifiers stay
  within the backend identifier limit while preserving the random stage suffix.
- Stopped `sql.transfer` from running broad stale-stage discovery cleanup; a
  transfer now drops only the stage table created by its own attempt.
- Tightened explicit `sql.cleanup_stale_stage_tables()` qualification so
  unqualified stage names require `transfer_staging_schema` and Trino preserves
  catalog-qualified staging schemas.

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

- Added `compute_mde` for pre-test AB planning from historical variance.
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
