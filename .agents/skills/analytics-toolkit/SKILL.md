---
name: analytics-toolkit
description: "Use whenever a task reads or writes warehouse data in Trino, ClickHouse or Greenplum, even when analytics-toolkit is not mentioned: Python analytics scripts and notebooks, ad hoc sql.read checks, schema and partition discovery, writing, running or printing SQL for a script, DAG or ticket, backfills, CSV and Excel exports, AB tests, SQL formatting, reporting dates and timestamps, and project-relative files and logging. Prefer matching toolkit helpers over equivalent pandas or stdlib utilities."
---

# Analytics Toolkit

Use the public `analytics_toolkit` APIs for the capabilities they already cover.
Apply this preference to consumer code, including scripts, notebooks, and usage
examples, and to SQL that you write, run, or print for warehouse tasks,
including schema and partition discovery. Explicit user choices take precedence.

## SQL rules

These rules apply to all SQL for Trino, ClickHouse, or Greenplum: scripts, DAG
SQL, probes, and SQL you only print. Before writing SQL, read
[SQL helpers](references/sql.md) for call semantics and
[SQL practices](references/sql-practices.md) for recipes.

### Partitions first

- Before writing SQL against a table, look up its partition key; for ClickHouse
  also the sorting key, for Greenplum also the distribution key. Do this once per
  table per task with the [partition discovery](references/sql-practices.md#partition-discovery)
  recipes, and name the keys in your plan or summary.
- Filter every scan of a partitioned table on its partition column with
  literals or a half-open range (`dt >= date '2026-10-01' and dt < date '2026-11-01'`).
  For transform partitions such as Iceberg `day(ts)` or ClickHouse
  `toYYYYMM(dt)`, filter the source column. Filters on other columns do not
  prune, and join or subquery filters are not a reliable substitute. When the
  user asks for a filter on another date, keep it and add a partition range that
  covers it.
- Align loops, batches, and delete or replace windows with partition boundaries.
- If a large table has no usable partition key, say so before running a heavy
  query, and bound the scan another way: a sorting-key filter, a sample, or one
  recent period.

### One processing stage, one call

- Keep the statements of one processing stage (stage rebuild, delete, insert,
  cleanup, Trino `set session`) in one SQL string or file and run it with one
  `sql.execute` call. Do not split SQL on `;` yourself, and do not create a call
  or a file per statement.
  - Trino splits the string and runs the statements in order in one session,
    so `set session` carries over.
  - ClickHouse splits it and runs the statements in order, but each statement is
    a separate request without a session: `SET` and temporary tables do not
    carry over. Use the statement's `SETTINGS` clause and regular stage tables.
  - Greenplum sends the whole string as one transaction, so an error rolls it
    all back. Keep the default; `gp_break_query=True` runs the statements one by
    one, still in one transaction unless `gp_commit_each_statement=True`.
- `sql.read` runs exactly one statement. When setup statements end in a SELECT,
  pass the whole script to `sql.execute_read` (returns a DataFrame),
  `sql.execute_insert` (inserts into an existing table), or `sql.execute_create`
  (creates a table); they run on one connection. Do not chain calls or open a
  raw cursor for this.
- Pass a list for independent items, such as per-partition chunks. Each item
  gets its own connection and retries, and items share no session state.
- Use a separate call only for a concrete reason, such as a step that a retry
  must not replay, and say why in a comment.

### Retry-safe writes

- Keep the default `retry_cnt=5`; do not lower it to work around SQL that is
  unsafe to rerun. After a transient failure, `sql.execute`, `sql.insert`,
  `sql.execute_insert`, and `sql.execute_create` rerun the whole call on a fresh
  connection from its first statement, even when some writes already committed.
  - Trino reruns statements that already finished.
  - Greenplum rolled the failed transaction back, so the rerun starts clean
    unless the commit itself was lost or `gp_commit_each_statement=True` is set.
  - ClickHouse stops retrying once a write was submitted and raises. A manual
    rerun still replays the whole script.
- Make every call safe to replay: clear the target slice first, then write it
  (drop or truncate the partition, or delete the slice, then insert). Start stage
  tables with `drop table if exists` before `create`. Do not write bare appends.
  Completed list items are not replayed.

### Replacing data

- On every backend, prefer partition operations to row deletes: drop, truncate,
  or recreate the affected partitions, then insert. Recipes are in
  [replacing data](references/sql-practices.md#replacing-data).
- ClickHouse: never use `DELETE FROM` or `ALTER TABLE ... DELETE`. Replace data
  only by dropping partitions and inserting, or with `REPLACE PARTITION ... FROM`
  a stage table. `sql.drop_partitions` handles toolkit `<table>_shard` pairs; for
  other tables, run `alter table <local table> on cluster '<cluster>' drop partition '<value>'`
  through `sql.execute`.
- ClickHouse: if the reload unit does not match the partition key (for example,
  reloading some users in a month-partitioned table), stop and ask the user
  before writing. Offer options such as rebuilding the affected partitions into
  `<table>_new` and swapping the names.

### Cost and shared clusters

- Keep exploratory, sizing, and diagnostic queries small: one partition unless
  the question needs more, only the needed columns, and samples for rough
  profiling. Never sample for reported metrics or test-vs-control comparisons.
  `row_limit=` caps the rows returned, not the rows read.
- Answer coverage, size, and freshness questions from partition metadata before
  scanning data. Scan each heavy partition once and derive all outputs from it.
- Before launching anything heavy, say in one line which tables and partitions
  it reads and why.
- Compute ordinary stages for the whole period in one set-based statement with
  a partition range. Loop one partition per query only for heavy tables or after
  a statement hit a memory or time limit, and pass those queries as a list.
- Run large backfills and rebuilds in partition-aligned batches, usually month
  by month, limited to periods that hold source data. Batch every heavy step,
  including the final publish or copy. Run DDL such as `add column` once for
  the whole table.
- Treat a concurrency limit the user sets as fixed. Raise it only with the
  user's permission and while no other users' queries are running. List input
  defaults to `concurrency=1`; `sql.async_sql` and `sql.parallel_sql` default to
  5. While a long job runs under your database user, keep diagnostics tiny,
  because they share that user's memory and concurrency limits.
- For long jobs, follow [long jobs](references/sql-practices.md#long-jobs):
  batches and ETA up front, per-batch progress, labels, and cleanup of leftover
  queries before a restart.

### Print-only requests

- When the user asks to print, provide, or write SQL or a script, deliver it
  without running it. Do not execute it, EXPLAIN it, or otherwise validate it
  against a database, and do not create tables, seed data, or start backfills
  unless asked. Read-only metadata lookups needed to write it (columns,
  partitions) and offline checks remain fine. Say that nothing was run, and
  offer a one-partition test run.
- Put printed SQL inline, complete and runnable, with template parameters
  filled in, not as a fragment or only a file path. A requested script still
  goes to its file.

## Consumer script preferences

These preferences apply to this user's analytics consumer code; they do not
change the toolkit's implementation contracts.

- Keep one-off scripts as direct top-level steps. Avoid `main()` wrappers,
  `if __name__ == "__main__":` guards, and CLI scaffolding unless requested or
  required by the existing project. Add functions only for useful reuse or a
  calculation that benefits from a separate function.
- Avoid `try/finally` unless failure would leave a resource or external state
  that requires cleanup or restoration. Keep ordinary analytics steps
  sequential and straightforward.
- Prefer `DataFrame.query(...)` for row filtering over boolean masks,
  `.eq(...)`, or other comparison-based indexing. For example, use
  `data.query("period_type == 'exp'")`. Reference Python variables with `@name`.
  [Result types](references/sql.md#result-types) lists the dtypes that break
  comparisons.
- Preserve toolkit parameter defaults unless the user requests different
  settings, including the statistical settings and layout of a requested
  reference report. A reference report does not justify extra breakdowns; ask
  before copying them. Do not change outlier treatment, comparison modes, or
  other analysis options based on an unsolicited judgment.
- Rely on automatic `.connections` discovery when it finds the intended file.
  Use `set_connections_path(...)` only when discovery is insufficient or an
  explicit path is requested.
- Never open, print, parse, grep, copy, or rewrite `.connections` or `.secrets`,
  and never move their values into environment variables or temporary config
  copies. Refer to aliases by name and list them with `sql.validate_connections()`.
  If a task needs another host, cluster, or alias, ask the user to add an alias.
- In deliverable code (scripts, notebooks, DAGs), do not add custom checks that
  raise (`ValueError`, `RuntimeError`, `assert`, and so on) or speculative
  guards unless the user requests them. Let anomalies flow through the task
  logic, and drop them only when the user or the existing logic says so. Log
  counts you already have with `time_print(..., level="warning")` and continue.
- Do not add expected-versus-actual count reconciliations, count-only queries,
  diagnostic SQL, or validation stages unless requested, and do not run lengthy
  validation of tables the user treats as trusted. Do not guard
  toolkit-managed operations either, for example by polling
  `system.distributed_ddl_queue`. Keep the toolkit's built-in validation,
  exceptions, and defaults, such as `sql.transfer(validate_row_count=True)`; do
  not remove, replace, or suppress them unless the user requests it.
- Use the toolkit's file and path helpers instead of `pathlib` in consumer code.
- Keep a script's queries in numbered `.sql` files under `sql/` and load them
  with `read_file_here(path, params)`; see
  [SQL files and numbering](references/sql-practices.md#sql-files-and-numbering).
  Make multi-step and multi-period scripts restart-friendly: each step replaces
  only its own slice, and a relaunch skips finished slices
  ([restart-friendly scripts](references/sql-practices.md#restart-friendly-scripts)).
- When the user names a reference project or script, copy its SQL formatting,
  file layout, and Python structure in the first draft. Use direct top-level
  `sql.read` and `sql.execute` calls with the SQL in files. Add no wrapper
  helpers, imports, or report files that the reference does not use, unless the
  task needs an operation only they provide. Keep the SQL file numbering rule
  even when the reference numbers files differently.
- When a requested data check fails, report it with `time_print`: the message,
  then the offending rows with `time_print(df.to_string(index=False))`. Do not
  write JSON or report files. Stop the run afterwards when later steps depend on
  the check, and let toolkit exceptions propagate.
- In ad hoc and ticket scripts, keep `time_print`'s default `print` sink. Call
  `set_time_print_sink("logging")` only in Airflow DAG modules; without a logging
  configuration, Python drops the toolkit's info messages.
- To quiet toolkit connection and timing lines in probes, call
  `set_time_print_level("warning")` from `analytics_toolkit.general` instead of
  filtering output with `grep -v`, which can hide result rows. The level applies
  to the whole process and also hides your own info-level `time_print` calls, so
  print results with `print`. Keep the default level in long jobs so progress
  stays visible.
- Do not write test files or pytest suites for ticket or ad hoc analysis
  scripts unless asked; DAG and DataLens work follow their own skills. Validate
  cheaply instead: `sql.execute(db_key, script, dry_run=True).sqls` shows how the
  toolkit splits a script without connecting, and `sqlglot.parse(sql, read="trino")`
  (or `"clickhouse"`, `"postgres"`) is a parse smoke check. When running
  queries is allowed, validate one statement with `explain (type validate)` on
  Trino or `explain syntax` on ClickHouse. When execution is authorized,
  smoke-test one partition or a short date range before the full run.

## Exploration and results

- Read-only exploration with `sql.read` on the configured aliases is
  pre-authorized. Look up schemas, columns, partitions, coverage, and sample
  values yourself instead of asking the user or asking them to run queries.
  `sql.read` executes any single statement, so keep exploration to `select`,
  `show`, `describe`, and `explain`, and cap samples with `row_limit=`. Writes
  still need approval. Ask only about business definitions that the data and
  existing code do not settle.
- If a read fails with a DNS or connection error in a sandbox, rerun it with
  network access before reporting a blocker. Do not use browser or desktop
  automation to inspect data or running queries.
- Before defining business logic for a table (definitions, formats, matching
  rules) or guessing table names, search existing code in the workspace:
  earlier ticket or analysis projects, newest first, and the DAG that builds the
  table (search the DAG repository for the table name). Reuse or propose the
  newest established logic, and ask only if nothing is found.
- For exploration, prefer prepared marts and provider tables close to the report
  over raw event logs. Do not switch a report's or job's source without checking
  its coverage and asking.
- Before reporting a metric, check that its magnitude is plausible (per user,
  per day, against known baselines) and flag zeros or extremes.
- When derived numbers contradict the user's established results, audit your
  own pipeline first: row counts of each stage and mapping table, join keys and
  types, date windows and filters, and how cohort tables were built (joins that
  depend on the outcome bias results). Question the established result only
  after that. Label untested explanations as hypotheses.
- Base funnels and cohort metrics on the full assigned population, with stage
  flags left-joined. Compare cohorts with benchmarks built from the same
  eligibility filters.
- These are reviews of results, not checks added to deliverable code.

## Toolkit problems in consumer work

- When a toolkit failure blocks consumer work, diagnose and explain it first,
  and change nothing in the toolkit until the user asks. Do not edit the
  toolkit repository from a consumer session; when the user wants a fix, write a
  bug-report prompt for a toolkit-repository session.
- Prefer a task-local workaround in the task SQL or script, such as casting
  problematic columns to text, and keep using the toolkit helpers. Ask before
  changing toolkit defaults or connection configuration. Patch the installed
  package only when the user asks: back up the file first and list the patch in
  the bug report.
- Keep a list of toolkit issues met in the session. When asked, write an English
  bug-report prompt for the toolkit-repository agent. Include the installed build
  (the version plus its install source from `pip show` or `direct_url.json`,
  because the version alone does not identify the code), the backend and
  relevant non-secret options, exact errors, a minimal reproduction, and the
  workaround used. Never include credentials or `.connections` contents.
- Before debugging toolkit internals, check read-only whether the toolkit's dev
  branch already has a fix: `git log origin/dev -- <module>`, `docs/CHANGELOG.md`
  under `## Unreleased`, or a diff of the installed module against a checkout.
  Ordinary fixes do not change the version. Do not run the toolkit repository's
  startup or commit workflow from a consumer session.

## Select a helper before implementing the operation

1. Check the target project's dependency declarations and Python environment for
   the `analytics-toolkit` distribution (`atk` provides its convenience imports).
   The public SQL wrappers have no docstrings: use the signature card in
   [SQL helpers](references/sql.md) and the linked function reference instead
   of reading toolkit source, and check an unusual argument with
   `inspect.signature`. Installed versions can differ from the references.
2. Find the public helper matching the requested operation. When its behavior
   fits, use it instead of rebuilding the operation with pandas, `datetime`,
   `dateutil`, backend clients, or custom utilities.
3. Check output types, calendar normalization, endpoint inclusion, timezone and
   precision requirements before choosing a date or timestamp helper. Read
   [dates and timestamps](references/dates-and-timestamps.md) for these tasks.
4. For SQL, read [SQL helpers](references/sql.md) and
   [SQL practices](references/sql-practices.md). For SQL formatting, AB
   analysis, Excel output, or file/logging tasks, read the relevant section of
   [other helpers](references/other-helpers.md); its Excel and AB sections
   include the rules for report deliverables.

If the package or required public helper is unavailable, identify the helper and
propose adding or updating the dependency through the project's normal workflow.
Do not silently replace it with another implementation or install it solely
because this skill was invoked. If adding it is declined or incompatible with
project constraints, use an appropriate alternative and explain the reason
briefly.

Pandas remains appropriate for dataframe construction, filtering, aggregation,
and vectorized operations that the toolkit does not cover. Stdlib or other
libraries remain appropriate for capabilities whose semantics the toolkit cannot
preserve. State the concrete mismatch when choosing such an alternative for an
operation near the toolkit's capabilities.

## Public API routing

| Task | Shortcut from `atk` | Read when needed |
| --- | --- | --- |
| Calendar dates and reporting periods | `dt` | Dates and timestamps reference |
| Timestamps and time-preserving arithmetic | `dttm` | Dates and timestamps reference |
| Configured SQL reads, execution, loading, transfer, metadata | `sql` | SQL helpers; SQL practices |
| SQL formatting and query rewrites | `sql_format` | Other helpers: SQL formatting |
| Experiment comparisons, splitting, MDE | `ab` | Other helpers: AB analysis |
| Grouped or pivoted Excel reports | `excel` | Other helpers: Excel |
| Project-relative files and timestamped logging | `here`, `from_here`, `read_file_here`, `write_file`, `time_print`, `get_time_print_sink` | Other helpers: Files and logging |

Prefer `from atk import *` when possible in consumer scripts, notebooks, and
examples. Then use bare shortcuts such as `dt.get_today()`, `dttm.add_hours(...)`,
`sql.read(...)`, `ab.compute_test_metrics(...)`, and `excel.break_table(...)`.
The shortcuts expose the corresponding public `analytics_toolkit` objects.
`from atk import *` provides exactly `ab`, `dt`, `dttm`, `excel`, `sql`,
`sql_format`, `pd` (pandas, so do not import it again), `here`, `from_here`,
`read_file_here`, `write_file`, `time_print`, `get_time_print_sink`, and
`set_connections_path`. Import `read_file`, `set_time_print_level`, and
`set_time_print_sink` from `analytics_toolkit.general`.

When project rules disallow wildcard imports, the installed version lacks `atk`,
or the context needs explicit imports, use public module or function imports,
such as `from analytics_toolkit import sql`. Avoid deep implementation imports
and underscore helpers in consumer code.

When editing the toolkit itself, follow its `AGENTS.md`, module contracts, and
repository-local MCP workflow. Internal code implementing a helper may need
pandas or stdlib primitives; do not make it call itself to satisfy the consumer
preference. In other projects, follow their own instructions and dependency
workflow; using this skill does not require the toolkit repository's MCP server.
