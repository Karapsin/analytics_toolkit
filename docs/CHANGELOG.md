[Documentation overview](README.md)

# Changelog

Generated from package version bumps and recent commit history.

## Unreleased

- Use native ClickHouse row encoding for nullable timezone-aware inserts.
- Cast Trino VALUES parameters to validated stage types.

## 1.3.11.9 - 2026-08-13

- Corrected fail-fast integration retry counts to one attempt.
- Kept the cross-backend transport round-trip independent of ClickHouse layout semantics.
- Accepted NumPy integer query IDs in SQL cancellation.
- Kept integration query cleanup artifacts JSON-safe for NumPy IDs.
- Made Greenplum partition normalization idempotent for typed specs.
- Reserved Greenplum stage identifier space for internal row types.
- Adapted structured JSON values for Greenplum batch inserts.
- Prevent Greenplum concurrent staging index collisions.
- Preserve JSON array values in Greenplum transfer inserts.
- Finalize every Greenplum worker stage during concurrent upserts.

## 1.3.11.8 - 2026-08-13

- Use authentication-neutral TCP readiness probes for auth-configured Trino services.
- Resolve integration Airflow connections through its explicit environment secrets backend.
- Stabilized real Airflow connection routing in SQL integration coverage.
- Initialized all Airflow ORM models before integration connection resolution.
- Kept Airflow SQL integration dependencies compatible with current MCP tooling.
- Split HTTP and native core SQL integration into independent CI jobs.
- Allowed split core SQL transport checks to finish on hosted CI runners.
- Normalized Trino timestamp values to target column precision before inserts.
- Aligned Hive Parquet staging timestamps with Trino microsecond precision.
- Made representative cross-backend SQL integration failures fail fast.

## 1.3.11.7 - 2026-08-12

- Validated every ClickHouse integration profile across HTTP and native transports.
- Fixed Greenplum integration health detection on clean CI runners.
- Waited for stable Greenplum readiness before integration tests.
- Connected Greenplum readiness probes over authenticated TCP.
- Copy integration auth certificates from their root-owned generator container.
- Stream SQL integration certificates through a root container read for portable Docker behavior.
- Gate SQL auth tests on consecutive end-to-end Greenplum mTLS readiness checks.
- Terminate Greenplum mTLS natively and proxy its PostgreSQL negotiation with TCP passthrough.
- Allow the Greenplum init user to install the ephemeral TLS server key securely.
- Persist SQL integration startup and Greenplum mTLS readiness diagnostics.

## 1.3.11.6 - 2026-08-12

- Fixed source-staged Trino transfers to honor Parquet mode precedence.
- Made Trino Parquet staging DDL defaults independent from SQL staging defaults.
- Stopped sql.transfer from sending internal metadata in every row.
- Validated SQL API documentation signatures and preserved wrapped changelog entries.
- Preserved UUID and timezone-aware timestamp values in Trino Parquet staging.

## 1.3.11.5 - 2026-08-10

- Batch Greenplum AB metric queries within the slice limit.
- Added optional native ClickHouse protocol support.
- Hardened staged transfers and standardized stage names across SQL backends.
- Added pipelined keyed SQL staging with bounded concurrency, validation, and ETA logging.
- Redesigned ClickHouse reconfiguration topology and defaults inputs.
- Validated ClickHouse Distributed routing coverage and configurable DDL readiness deadlines.
- Based transfer throughput and ETA on active batch loading time.
- Retried fresh ClickHouse transfer finalization before source reloads.
- Added strict Trino S3 staging schemas, credential aliases, and custom endpoints.
- Made mandatory pre-commit checks staged, resumable, cached, and parallel.

## 1.3.11.4 - 2026-07-31

- Added segmented SQL AB metrics report helper with optional Excel output.
- Added concurrent list execution to sql.execute with soft and hard concurrency caps.
- Resolved here paths from active Positron editor execution metadata.
- Supported compound queries in Greenplum temp-table rewrites.
- Required explicit Greenplum connection keys for gp_vacuum.
- Made create_sql_table replace existing targets for every schema source.
- Renamed the AB metrics report helper, made segmentation optional, and fixed
  multi-metric SQL composition.
- Replaced ambiguous AB control/test output columns with group-position names.
- Stop retries for duplicate SQL result columns.
- Fixed ClickHouse replicated DDL, sharding expressions, and deterministic retries.

## 1.3.11.3 - 2026-07-30

- Added setup and read phases to execute_read timing logs.
- Allowed repeated SQL transfer key placeholders.
- Stopped retries for deterministic ClickHouse conversion failures.
- Split keyed SQL transfer reader and writer concurrency with a bounded observable pipeline.
- Added two-phase keyed source staging and per-call source-stage bypass.
- Fixed UUID inserts and deterministic SQL retries.
- Improved SQL transfer concurrency, logging, stage reliability, and portable metadata filters.

## 1.3.11.2 - 2026-07-28

- Preserved native UUID types across SQL backends and lowered default concurrency caps to five.

## 1.3.11.1 - 2026-07-28

- Avoided ClickHouse transfer stream failures for empty validated sources.
- Added per-connection DDL creation policies for Greenplum, Trino, and ClickHouse.
- Redesigned SQL transfer staging around immutable ordinal snapshots and shared transfer identities.
- Kept Trino timestamp normalization scoped to transfer source metadata.
- Expanded cross-backend transfer integrity coverage for values, schemas, Parquet staging, and range retries.
- Removed legacy SQL compatibility modules and consolidated backend imports.

## 1.3.11.0 - 2026-07-23

- Stabilized concurrent keyed transfer test ordering.
- Bounded first-poll GitHub status receipts.
- Removed duplicate commit receipt metadata.
- Added Excel file export support to sql.read dataframe results.
- Add concurrent Greenplum leaf-partition analyze helper.
- Avoid duplicate source query execution during validated SQL transfers.
- Default SQL transfers to append unless write_mode is explicit.
- Reduced agent workflow token and retry costs.
- Fixed workflow metrics repeated-failure aggregation.
- Stopped retries for deterministic SQL configuration errors.

## 1.3.10.19 - 2026-07-20

- Expanded deep SQL integration recovery, cancellation, type, atomicity, authentication, orchestration, and DDL coverage.
- Expanded deep SQL integration recovery, cancellation, types, atomicity, authentication, orchestration, DDL, and stress coverage.
- Added inline Greenplum range and list partitions to SQL table-creation workflows.
- Made async_sql Ctrl+C cancel queued and active database work.
- Added configurable sql.read output types with direct dictionary reads.
- Reduced agent workflow output and added implementation preflight diagnostics.
- Avoided duplicate nested command output in git workflow responses.
- Added ClickHouse table reconfiguration helper.
- Preserved Trino complex query types and stopped deterministic type mismatch retries.
- Reduced agent MCP response sizes and added fail-fast validation.

## 1.3.10.18 - 2026-07-15

- Completed deterministic ClickHouse lifecycle, DML, and metadata coverage.
- Completed shared and Greenplum backend contract coverage.
- Completed deterministic SQL load and table lifecycle coverage.
- Expanded deterministic SQL transfer coverage.
- Completed deterministic SQL execution and orchestration coverage.
- Completed the reviewed residual coverage ledger and removed dominated guards.
- Added disposable Greenplum, Trino, and ClickHouse integration environments.
- Expanded SQL integration profiles, coverage matrix, and exact-SHA GitHub verification.
- Made SQL identifier tests independent of local connection files.
- Expanded exhaustive SQL integration, authentication, fault recovery, and exact-SHA verification.

## 1.3.10.17 - 2026-07-14

- Raised SQL backend adapter coverage.
- Completed SQL adapter default policy coverage.
- Raised deterministic Excel and general helper coverage.
- Raised deterministic AB MDE planning coverage.
- Completed deterministic SQL formatting coverage.
- Completed deterministic AB utility branch coverage and simplified unreachable allocation guards.
- Made deterministic coverage target updates atomic and added check-only validation.
- Expanded deterministic transfer runtime and retry coverage.
- Expanded deterministic Trino backend coverage and cursor cleanup.
- Raised deterministic ClickHouse adapter and table-creation coverage.

## 1.3.10.16 - 2026-07-13

- Added exact statement, branch, combined, and prefix coverage enforcement.
- Raised AB utility coverage and fixed NaT MDE validation.
- Raised SQL formatting coverage and added automatic one-way coverage-floor ratcheting.
- Raised SQL connection and DDL coverage with behavioral tests.
- Raised SQL transfer finalization and row-count coverage.
- Completed SQL transfer option boundary coverage and removed an unreachable check.
- Completed Parquet transfer staging coverage.
- Completed SQL transfer source streaming coverage.
- Raised SQL transfer attempt orchestration coverage.
- Expanded SQL dataframe loading coverage.

## 1.3.10.15 - 2026-07-13

- Removed hard-coded Trino parquet load adapter use.
- Added date comparison helpers.
- Added additional date period helpers.
- Added analytics_toolkit.datetime timestamp helpers.
- Added opt-in here path resolution to read_file.
- Added general.set_connections_path for explicit SQL .connections file selection.
- Added from_here and read_file_here general path helpers.
- Calibrated bootstrap inference, hardened SQL workflows, and added quality gates.
- Added explicit force-release support for version workflows.

## 1.3.10.14 - 2026-07-07

- Moved generic SQL analyze support policy behind backend adapters.
- Adapterized SQL transfer early target creation policy.
- Keep SQL upsert planning policy adapter-owned.
- Adapterized generic SQL IO dispatch.
- Adapterized SQL load insert dispatch.
- Adapterized SQL identifier quote and dialect policy.
- Adapterized ClickHouse expected column metadata implementation.
- Adapterized SQL write-mode validation.
- Kept SQL insert chunk defaults backend-owned.
- Adapterized SQL ClickHouse option normalization.

## 1.3.10.13 - 2026-07-07

- Adapterized Greenplum stage table identifier policy.
- Adapterized create_table_from_sql direct insert policy.
- Adapterized ClickHouse create-table expected column metadata policy.
- Adapterized Trino load and transfer connection defaults.
- Adapterized SQL query create-table target policy.
- Adapterized ClickHouse create-table option policy.
- Adapterized Greenplum transfer insert-page sizing policy.
- Adapterized Trino insert chunk-size option validation.
- Adapterized SQL upsert capability policy hooks.
- Adapterized SQL show_tables catalog filter policy.

## 1.3.10.12 - 2026-07-07

- Move SQL backend policy into adapters.
- Move ClickHouse lifecycle SQL into backend adapters.
- Move remaining SQL backend behavior into adapters.
- Move SQL backend helper ownership into adapters.
- Move remaining SQL backend helper ownership into adapters.
- Move SQL type and insert helper ownership into adapters.
- Move ClickHouse truncate and Trino config SQL ownership into adapters.
- Fixed Trino transfer replace to recreate target tables before staged inserts.
- Adapterized SQL transfer/load backend policy hooks.
- Adapterized SQL backend option policy hooks.

## 1.3.10.11 - 2026-06-24

- Quote discovered Greenplum stale stage cleanup names.
- Create GP and Trino SQL transfer targets before staging rows.
- Log transfer key values in keyed batch transfers.
- Fix Greenplum extract_ddl fallback.
- Added mandatory sql.transfer row-count validation.
- Added ClickHouse transfer stream-read retries with smaller retry batches.
- Handled empty missing-target SQL transfers.
- Added SQL show_queries helper.
- Redesign SQL upsert finalization for Trino and ClickHouse as partition-scoped replacement.

## 1.3.10.10 - 2026-06-23

- Tighten SQL backend registry guardrails.
- Continue SQL backend registry cleanup.
- Moved remaining SQL backend helper ownership into adapters.
- Fixed Greenplum transfer stage insert retries to refresh closed target connections.
- Fix SQL backend adapter autoreload compatibility.
- Use cancel and terminate for Greenplum query cancellation.
- Accept scalar string SQL key columns.
- Use fresh target connections for SQL transfer and load_df target actions.
- Added clean_all option to SQL stale stage cleanup.
- Allow clean_all stale stage cleanup without target_table.

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
