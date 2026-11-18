---
name: dag-writer
description: "Create or modify Apache Airflow DAGs for data pipelines and analytical reports, or convert reporting scripts into DAGs. Use analytics_toolkit for supported SQL, date, statistics, file, and logging operations."
---

# DAG Writer

Write direct, maintainable Airflow DAGs with explicit metric definitions and safe
refresh behavior. User instructions and repository requirements take precedence.
The conventions below are preferred defaults for new DAGs unless the user asks
otherwise. Preserve existing DAG conventions when editing them.

## Ground the change in the target project

- Read applicable repository instructions and inspect relevant DAG examples in
  the target project. Follow their deployment structure and utility calls.
- Review existing edits before changing files; preserve intentional changes and
  deletions. An example DAG is a reference, not permission to edit it.
- Preserve an existing DAG's formatting, names, imports, and SQL conventions.
  Limit refactoring to the requested scope. Apply requested simplifications to
  the implementation rather than renaming unnecessary abstractions.
- Keep metric definitions, source assumptions, attribution rules, and refresh
  coverage as short `#` comments in `dag.py` or its SQL files. Do not pass
  `doc_md` to `DAG()`, and add no README or documentation pointers. Keep code,
  SQL, comments, and tests consistent with the final behavior.
- During ticket, recovery, or recalculation work, change files only in the
  ticket folder. You may read DAG files, but edit the DAG repository only when
  the user asks for a DAG change. If the user rejects repository changes, revert
  only your own edits, then confirm with `git status` and `git diff` that only
  the user's earlier changes remain.

## Python layout and processing

- Keep Python logic in one `dag.py` and business queries in its `sql/` subfolder.
  Import libraries at the top of the file. Number new SQL files by stage in
  execution order without zero padding (`0_ddl.sql`, `1_sources.sql`,
  `2_publish.sql`).
- Use `#` comments for explanations and headings; match neighboring separators
  and spacing. Multiline strings remain appropriate for simple metadata reads.
- Group deployment settings, such as connection keys and table names. Keep
  column names explicit in SQL rather than defining Python constants for them.
- Use ordinary arguments, local variables, and short `if` blocks. Avoid generic
  run plans, context dictionaries, query frameworks, and configuration layers
  that add no needed behavior.
- Call existing utilities directly; do not wrap `read_file_here`, `sql.read`, or
  `sql.execute` solely to shorten their calls.
- Extract a helper when it owns a meaningful calculation or processing step;
  keep its natural preprocessing and calculation together. Return lists for
  small date-batch collections; use generators when streaming serves a need.

## Required toolkit helpers and logging

Require `analytics_toolkit` for supported operations. Before implementation,
read the sibling [analytics-toolkit skill](../analytics-toolkit/SKILL.md) to
select public helpers and verify their contracts in the target environment.
Keep that skill available beside this one when distributing them. If the skill
or package is missing, report the dependency and use the project's normal
dependency workflow; do not silently install it or replace supported operations.
Using the toolkit in a consumer project does not import the toolkit repository's
development workflow into that project.

For new DAGs, prefer explicit public imports and import only what is needed,
rather than the toolkit skill's general wildcard-import preference. Typical
imports are:

```python
from analytics_toolkit import sql, dates as dt, ab_utils as ab
from analytics_toolkit.general import read_file_here, set_time_print_sink, time_print

set_time_print_sink("logging")
```

- Use `time_print` with the logging sink for progress; avoid custom logger setup.
- Use `dt` for matching date arithmetic, comparisons, and period generation.
  Check output types, endpoint inclusion, calendar normalization, and timezone
  semantics before choosing a helper.
- Format dates explicitly: cast metadata dates to `varchar` for string output,
  or use `.strftime("%Y-%m-%d")` on date values. Do not use zero-offset arithmetic
  as a conversion.
- Use `ab_utils` for supported statistics. Add a calculation helper only for
  output the library does not provide.
- Verify signatures and defaults; omit arguments that merely repeat defaults.
  Use `sql.read`, `sql.execute`, and `sql.load_df` directly.

## SQL boundaries and defaults

- Apply the analytics-toolkit skill's SQL rules to every DAG query: look up
  partition keys and filter on them, run one processing stage per
  `sql.execute` call, make every call safe to replay, and replace data by
  partition (never with row deletes in ClickHouse). Its
  [SQL practices](../analytics-toolkit/references/sql-practices.md) hold the
  recipes and the shared SQL style.
- Keep business queries in SQL files. Python may substitute parameters through
  `read_file_here`; it must not construct statements, column lists, expressions,
  or predicates.
- Keep query structure and columns explicit. Prefer concrete projections over
  `select *` or `select alias.*`.
- Simple metadata reads may call `sql.read` with table-name substitution
  directly; do not introduce a generic `read.sql` for them.
- Group related statements by processing stage. `sql.execute` supports multiple
  statements in one query; a statement does not need its own file.
- Use lowercase keywords and neighboring indentation. Begin filtered queries
  with `where 1=1`, placing filters under `and`.
- Use positional grouping on one line, such as `group by 1, 2, 3`.
- Omit the target column list from `insert into`. Keep the source projection
  aligned with the destination schema, including when columns change.
- Escape substituted data literals correctly. Keep deployment identifiers
  separate from data values without adding a generic query framework.

Typical execution pattern:

```python
params = {"provider": PROVIDER, "stage": STAGE}
query = read_file_here("sql/1_refresh.sql", params)
sql.execute(CONNECTION_KEY, query)
```

## Schema changes and ClickHouse publication

- When a change needs new columns in existing tables, assume the columns
  already exist. Do not put `alter table ... add column`, other one-off
  migrations, or backfills of existing history (such as populating new columns)
  in DAG code. Keep `create table if not exists` for new tables, and keep the
  DAG's normal refresh and first-run logic. Write migrations and backfills as
  separate scripts in the ticket folder, and offer them.
- Publish Trino results to ClickHouse through permanent stage pairs: a
  Replicated `<stage>_shard` table and a Distributed facade, both created
  `on cluster` with the target's structure. In SQL files read with
  `read_file_here(path, params)`, write ClickHouse macros as `{{cluster}}`,
  `{{uuid}}`, `{{shard}}`, and `{{replica}}`. Never drop the permanent stages.
- Load a stage with one Trino `sql.execute` string:
  `set session <clickhouse catalog>.non_transactional_insert = true;` followed
  by `insert into <clickhouse catalog>.<db>.<stage> ...`. Keep both statements
  in the same string; list items use separate connections, so the session
  setting would be lost.
- Clear the stage with `truncate table <stage>_shard on cluster '<cluster>'`
  right before loading and again after publishing; truncating the Distributed
  facade does not remove shard data. Publish on the local tables with
  `alter table <target>_shard on cluster '<cluster>' replace partition <p> from <stage>_shard`,
  using `tuple()` for unpartitioned tables. Both tables need the same columns,
  partition key, order by, and storage policy. `exchange tables` (Atomic
  databases only) and truncate plus insert are alternatives.
- Do not add `sync` to `truncate`, and do not use `system flush distributed` or
  `system sync replica`; DAG accounts often lack these privileges. If a
  privileged command fails in production, remove it instead of adding
  workarounds or guards.
- Use the Trino connector insert in DAGs, with `sql.transfer` only as a
  fallback when the connector is unavailable. Into existing stage tables, pass
  `write_mode="truncate_insert"` or `"append"`, because replacing an existing
  replicated target runs `SYSTEM SYNC REPLICA`.

## Airflow orchestration and retries

- For a simple serial pipeline, prefer one task calling pipeline functions in
  sequence. Add task boundaries when they provide an operational benefit.
- Split a heavy multi-stage pipeline into stage tasks (for example ddl,
  sources, ..., publish, finalize, chained with `chain`) when a failure in a
  later stage would otherwise rerun long earlier stages. An Airflow retry then
  reruns only the failed stage. Pass data between stages through tables, not
  XCom, and keep each stage safe to retry.
- Run one-off backfill speed-ups outside the DAG and leave the scheduled path
  unchanged unless asked. Use toolkit concurrency (`sql.execute` with a list of
  independent batch queries and `concurrency=N`, or `sql.parallel_sql`) instead
  of hand-written thread runners.
- Before a long local run that reuses a deployed DAG's SQL, check the DAG's
  schedule. Ask the user to pause it, or give the local run its own stage names
  while the DAG code keeps its fixed names; a scheduled run can drop shared
  stage tables.
- Avoid `ti`, XCom plumbing, and task context when ordinary calls suffice.
  Disable XCom return storage when unused.
- Do not add upstream dependencies, sensors, `dag_run.conf`, manual date
  overrides, or configurable refresh dates unless requested.
- For reports through the current day, use `today = dt.get_today()` and the
  exclusive end `dt.add_days(today, 1)`.
- Prefer fixed staging table names with `max_active_runs=1` for serialized
  execution. Avoid run hashes, unique suffixes, or stored run IDs without a
  concrete need; concurrent runs must not share mutable stages unsafely.
- Preserve library retry defaults and make each write safe to replay: clear
  the slice, then insert. Lower retries only when a write cannot be made
  replay-safe, such as an append after a lost acknowledgement, and explain the
  exception beside the call.
- Make whole-task retries safe: recreate ordinary stages, replace affected data
  consistently, and retain necessary pending work until success. Permanent
  ClickHouse publication stages are truncated, not dropped.

## Incremental state and affected results

Apply these rules when the pipeline needs persistent or incremental state:

- Store only state needed for coverage, incremental updates, and retries.
  Distinguish the earliest calculation history from the current refresh window.
- Reuse persistent intermediate facts when they avoid expensive repeated source
  reads; choose their grain from the calculations they support.
- Select affected entities and periods in SQL. Replace cached metrics and
  results using consistent keys while preserving unaffected history.
- Account for additions, removals, membership changes, baseline changes, and
  delayed or corrected events. Select affected periods using report attribution;
  an event's date alone may miss an earlier reporting period.
- Drive incremental refresh windows and batches by business date columns,
  ideally the source's date partition columns. Find the partitioning with the
  analytics-toolkit
  [partition discovery](../analytics-toolkit/references/sql-practices.md#partition-discovery)
  recipes. Do not drive refresh by Iceberg snapshot IDs, time travel, or
  technical load timestamps. Catch late data by re-reading a recent window of
  those dates.
- If a source has no business date or date partitioning (for example, only
  `bucket(id, n)`), say so and propose an alternative for the user to approve,
  such as refreshing from changed data files.
- On retries, re-read the source date ranges instead of reusing stale staged
  copies. Record pending changes before overwriting the evidence needed to
  detect them; clear pending work only after successful publication and state
  updates.
- Derive entity counts and groups from source data. Deduplicate shared facts at
  the required grain before aggregation so shared identifiers do not multiply
  joins.
- Recompute a whole affected comparison when calculations need its complete
  cohort. Updating only new users is insufficient for clipping, means, or CUPED
  coefficients.
- Note refresh coverage and limits in a `#` comment and in your final message.
  A recent refresh window does not necessarily detect historical corrections.

## Dates, metrics, and statistics

- Use half-open date ranges: include the start and exclude the end. Define
  reporting periods, eligibility windows, baselines, and event attribution from
  the requested metrics and keep them distinct in Python and SQL.
- Preserve reference calculation boundaries when converting scripts. Broader
  source lookup can change results even with unchanged reporting dates.
- Use the requested weekly calendar convention and handle partial weeks. Do not
  split calendar weeks at month or year boundaries unless required.
- Retain zero-activity participants when they belong in the denominator. Keep
  baseline activity separate from reported outcomes.
- Follow library behavior for undefined statistics and allow its warnings to
  surface. Do not add warning suppression, p-value checks, or statistical
  fallbacks without a concrete requirement.
- Avoid extra runtime validation, `ValueError` guards, memory guards, and
  validation stages unless required. Keep report schemas limited to requested
  fields; document operational details outside the report output.

## Verification and delivery

- Deliver only `dag.py` and its `sql/` files to the DAG repository. Do not add
  tests (including to an existing `tests/` folder), check or diagnostic SQL,
  patches, READMEs, or other verification files there. Keep tests and
  verification artifacts outside the repository, in the task's working folder
  (for example, the ticket folder).
- Test meaningful behavior using synthetic data or isolated substitutes. Cover
  changing source sizes, relevant date boundaries, affected-period selection,
  shared identifiers, and retries when the change involves them. Do not add
  tests solely for trivial formatting changes.
- For reference comparisons, exercise metric-building SQL as well as final
  aggregation and statistics. Distinguish saved-snapshot checks from live-data
  verification.
- Choose tolerances appropriate to the metric and task. For approximate checks,
  focus on material differences and changes in conclusions; require exact
  agreement only when the task or data contract calls for it.
- Use authorized connection discovery and `sql.read` for source inspection.
  Discovery permission does not authorize live writes. Keep credentials out of
  code, documentation, and output.
- Align tests with intentional behavior changes. Run appropriate checks without
  repeating passing checks unless new changes or unresolved concerns warrant it.
- When requested, supply reset or cleanup SQL covering all pipeline-owned tables
  while preserving source tables. Supplying SQL does not authorize executing it.
- Leave no verification artifacts in the DAG repository, and report what
  changed, what was checked, and any material limitations.
