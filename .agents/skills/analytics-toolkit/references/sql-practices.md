# SQL practices

Recipes and working rules for warehouse SQL run through the toolkit. Helper
signatures and call semantics are in [SQL helpers](sql.md). Names in angle
brackets are placeholders; aliases in examples (`trino`, `ch`, `gp`) stand for
the project's configured aliases.

## Partition discovery

No toolkit helper returns partition keys. Use `sql.extract_ddl` or `sql.read`
on metadata, once per table per task. `sql.table_info` returns existence and
columns only, and `include_row_count=True` runs a `count(*)` that may scan the
table.

Trino

- `sql.extract_ddl("trino", "<catalog>.<schema>.<table>")` runs `SHOW CREATE
  TABLE`: Iceberg shows `partitioning = ARRAY[...]` and `sorted_by`, Hive shows
  `partitioned_by`. It can be denied on shared schemas and fails on views.
- With SELECT access only, read the partitions metadata table. It reads
  metadata, not data, and `json_format` works on every toolkit build:

  ```sql
  select json_format(cast(partition as json)) as p, record_count, file_count, total_size
  from <catalog>.<schema>."<table>$partitions"
  ```

  Field names such as `ts_day` or `valid_from_month` mean `day(ts)` and
  `month(valid_from)`, so filter the source column. A missing `partition`
  column means the table is unpartitioned. `"<table>$files"` gives per-file
  rows and bytes.
- Views: `show create view`, or `explain (type distributed)` on a filtered query
  to see the base table and whether the filter became a `constraint on [...]`.
- Hive or object-storage tables: `describe` marks partition columns with
  `partition key` in the Extra column.

ClickHouse

- MergeTree tables: `sql.extract_ddl("ch", "<db>.<table>")` shows `PARTITION
  BY`, `ORDER BY` and `SAMPLE BY`. Or read
  `select partition_key, sorting_key, sampling_key, engine_full from system.tables where database = '<db>' and name = '<table>'`.
- Distributed tables and views have empty keys, and their DDL is only the
  wrapper. `Distributed('<cluster>', '<shard_db>', '<shard_table>', ...)` names
  the local table, and a view's SELECT names its base tables.
  `sql.table_info(...).shard_table` only appends `_shard` to the name you pass;
  it does not read the engine. When the connected host has no copy of the local
  table:
  `select partition_key, sorting_key from cluster('<cluster>', 'system', 'tables') where database = '<shard_db>' and name = '<shard_table>' limit 1`.
- Rows, bytes and load times per partition: `cluster('<cluster>', 'system', 'parts')`
  filtered on `active` and the local table, grouped by `partition`. Plain
  `system.parts` covers only the connected host. Whole-table totals of a
  Distributed table:
  `sql.show_tables("ch", schema="<db>", table_name="<table>", ch_distributed_table_stats=True)`.
- Pass three quoted arguments to `cluster()` and `clusterAllReplicas()`; the
  toolkit rejects `cluster(<cluster>, system.tables)`. Under the default `stop`
  outage policy, a `cluster(...)` read of system tables fails while any host of
  that cluster is unreachable. For a metadata lookup,
  `ch_unavailable_shards="healthy_shards"` reads the reachable hosts with a
  warning; rows from unreachable hosts are missing.
- With `healthy_shards` (as an argument or the alias default), `SHOW CREATE`,
  `EXPLAIN` and `sql.extract_ddl` fail with "Ad hoc availability cannot be
  verified for this native command". Pass `ch_unavailable_shards="stop"` to them.
- For a view, filter in `where` on the column that maps to the base table's
  partition key. `prewhere` adds nothing for pruning and fails on views
  (ILLEGAL_PREWHERE).

Greenplum

- `sql.extract_ddl("gp", "<schema>.<table>")` returns the `DISTRIBUTED BY`
  clause and, where discoverable, the partition clause. Child partitions and
  their bounds are in `pg_partitions` (filter `schemaname` and `tablename`),
  partition columns in `pg_partition_columns`.
- Child partitions can exist while empty, so count rows per slice when you
  check coverage.

Checking pruning before heavy ad hoc SQL (not for print-only requests, and not
in committed DAG SQL):

- Trino: `explain` shows `constraint on [column]` on the table scan when the
  filter prunes partitions.
- ClickHouse: `explain indexes = 1` lists the partitions and parts selected.
  `settings force_index_by_date = 1` makes a MergeTree query fail when no
  partition-key condition can be used; it does not check selectivity.

## Replacing data

Every write call must be safe to replay (see
[retries](sql.md#retries)): clear the target slice first, then write it, in one
script run with one call. Prefer partition operations to row deletes. Never
drop or rebuild completed work on a rerun unless the user asks.

Trino and Iceberg

- A `delete` whose literal predicate covers whole partitions is metadata-only
  (`TableDelete` in `explain`); key or subquery deletes rewrite rows
  (`MergeWriter`). Delete partition-aligned literal ranges:
  `delete from <table> where dt >= date '2026-10-01' and dt < date '2026-11-01'`.
- `sql.drop_partitions("trino", table, ["2026-10-01"], trino_partition_column="dt")`
  runs `DELETE ... WHERE dt IN (DATE '2026-10-01')`, so it fits date-typed
  partition columns only.
- For upserts, see [upserts on Trino](#upserts-on-trino-without-merge).

Greenplum

- Truncate or drop the affected range partitions, then insert:
  `sql.drop_partitions("gp", table, ["2026-10-01"], gp_truncate=True)` truncates
  them, and without `gp_truncate` drops them, after which
  `sql.gp_create_partitions` recreates them. Inside one script,
  `alter table <table> truncate partition for ('2026-10-01');` followed by the
  insert runs as one transaction with one `sql.execute` call, so a failure
  rolls back both.

ClickHouse

- Never use `DELETE FROM` or `ALTER TABLE ... DELETE`. Replace data only by
  partition, on the local tables:
  1. Drop the affected partitions. Use `sql.drop_partitions("ch", "<db>.<table>", ["2026-10-01"])`
     for toolkit `<table>_shard` pairs. For other tables, run
     `alter table <db>.<local table> on cluster '<cluster>' drop partition '2026-10-01'`
     through `sql.execute`, taking the local table and cluster from the
     Distributed engine.
  2. Insert the slice.
- Or load a stage table with the same structure and swap the partition in
  atomically:
  `alter table <local table> on cluster '<cluster>' replace partition '<value>' from <stage local table>`.
  Both tables need the same columns, partition key, order by and storage
  policy; unpartitioned tables use `tuple()` as the partition.
- Partition values must be literals as listed in `system.parts.partition`;
  expressions such as `toDate(...)` fail (code 62).
- A failed insert can leave a partial partition. The replay-safe script drops
  the partition again before inserting.
- When the reload unit does not match the partition key (for example, some
  users across a month-partitioned table), stop and ask the user before
  writing. Options include rebuilding the affected partitions into
  `<table>_new` and swapping names (`exchange tables` on Atomic databases, or
  `rename`), or reloading whole partitions from the source.

## Upserts on Trino without MERGE

- Do not use `merge into` for batch updates or upserts on Trino/Iceberg. Use
  one script run with one `sql.execute` call:

  ```sql
  drop table if exists <schema>.<stage>;
  create table <schema>.<stage> as
  select ... from ... where dt >= date '{start_dt}' and dt < date '{end_dt}';
  delete from <schema>.<target> where dt >= date '{start_dt}' and dt < date '{end_dt}';
  insert into <schema>.<target> select ... from <schema>.<stage>;
  drop table if exists <schema>.<stage>;
  ```

  The leading `drop table if exists` lets a retry recreate a stage left by the
  failed attempt. Prefer literal partition-aligned deletes; key or subquery
  deletes rewrite rows. For DataFrame or cross-database loads, `sql.load_df` and
  `sql.transfer` with `write_mode="upsert"` already follow this pattern.
- Trino inlines a CTE at every reference. Keep logic in CTEs by default. When a
  CTE, or an expensive shared scan, is referenced two or more times,
  materialize it as a stage table that the same script creates and drops.

## Moving rows between databases

- Never inline row sets or long ID lists into SQL text (VALUES lists, long IN
  lists, array strings). The toolkit parses every statement with sqlparse,
  which rejects very long statements ("Maximum number of tokens exceeded").
  Stage the rows on the target backend and join there:
  `sql.transfer(from_db="gp", to_db="trino", from_sql=..., to_table="<sandbox schema>.<name>")`
  for a query result, `sql.load_df("trino", "<sandbox schema>.<name>", df)` for
  a DataFrame. Substituting scalars such as dates, or a short literal list, is
  fine.
- When one Trino query can read both sides through a connector catalog (for
  example, a ClickHouse catalog), copy the data inside Trino with
  `insert into ... select ... from <clickhouse catalog>.<db>.<table>` instead of
  streaming it through Python with `sql.transfer`. Make each partition-sized
  chunk a replay-safe script (delete the chunk's range, then insert it) and
  pass the chunks as a list, `sql.execute("trino", [chunk_1, chunk_2, ...])`.
  Items run in order with separate retries, and a failure stops the remaining
  chunks without rerunning finished ones; a single multi-statement string would
  rerun every chunk on a retry.
- When a Greenplum table also has a native Iceberg copy, read the copy in Trino
  rather than going through a Greenplum connector catalog. If no copy exists,
  say so and ask.

## Restart-friendly scripts

- Make every multi-step or multi-period data script resumable by default. Each
  step replaces only its own slice: one SQL script first removes the slice
  (Trino partition-range `delete`, Greenplum partition `truncate`, ClickHouse
  `drop partition`) and then inserts it.
- On relaunch, skip completed slices. Record each finished slice in a small
  progress table at the end of its script; this also covers slices with no rows
  and ClickHouse, where a failed insert can leave a partial partition. On Trino
  Iceberg, `"<table>$partitions"` is a cheap check when every slice has rows. On
  Greenplum, count rows per slice. Test whether a table exists with
  `sql.table_info(...).exists`, and create tables with `create table if not exists`.
- Use fixed stage-table names, not run-unique ones, and run one copy of the
  script at a time.
- Pass an explicit `write_mode` to `sql.transfer` and `sql.load_df`, because
  their default `replace` rebuilds the whole target.

## ClickHouse notes

- Toolkit-managed tables are pairs: a local `<db>.<t>_shard` and a Distributed
  facade `<db>.<t>`. Their engine and `ON CLUSTER` scope come from the alias's
  `ddl_defaults` or the `ch_*` arguments. `sql.create_table`,
  `sql.execute_create`, `sql.load_df` and `sql.transfer` build both by default;
  `ch_only_shard=True` creates one local table under the requested name. Keep
  the facade, because readers and dashboards query it. Tables created outside
  the toolkit may use other local names, so read the local table from the
  facade's engine.
- Hand-written facades need an explicit column list: `create table X ... as X_shard`
  fails on any host that lacks `X_shard`, such as a gateway outside the shard
  cluster. An alias may connect to such a gateway, so check `hostName()` and
  `system.clusters` (`is_local`) before `ON CLUSTER` DDL or shard-level writes.
- Outage policy: the toolkit default for `ch_unavailable_shards` is `"stop"`,
  but an alias can set another default. Under `"stop"`, incomplete coverage
  raises a non-retryable `ClickHouseClusterUnavailableError`; reads of
  replicated tables can fail over to another replica of the same shard. Drops,
  truncates, replacements and other destructive DDL need every host under
  either policy. `"healthy_shards"` reads or writes only the reachable shards
  and warns with `ClickHouseDegradedClusterWarning`. Pass it only for explicit
  ad hoc reads, never for backfills. A missing replica is not a missing shard;
  check `system.clusters` before saying data is lost.
- Do not set `skip_unavailable_shards` inline; a value that contradicts the
  policy raises `ClickHouseClusterTopologyError`.
- Bound cost with partition filters rather than `SETTINGS max_execution_time`;
  servers can forbid changing it (code 452, SETTING_CONSTRAINT_VIOLATION).
- `AmbiguousSqlReplaceError` ("destructive DDL may be partially applied") wraps
  any failure of a destructive statement submitted after a live write in the
  same call, even a syntax error. The server error is chained as `__cause__`.
  It is also in `system.query_log` (`type != 'QueryStart'`), or in
  `system.distributed_ddl_queue` for `ON CLUSTER` DDL.
- Memory limits apply per database user, so diagnostics share the limit with
  any running job of the same user.

## Trino and Iceberg notes

- Validate one rendered statement without reading data, when running queries
  is allowed: `sql.read("trino", "explain (type validate) " + query)`.
  `explain (type distributed)` shows the plan, estimates and scan constraints
  (`constraint on [dt]`).
- Sampling syntax is `from big_table as t tablesample system (1)`, with the
  alias before `tablesample`. `system` sampling keeps or drops whole splits, so
  the sample follows the file layout. If the user asks for a sampled
  comparison, use `bernoulli` or a hash of the unit id.
- Filter `information_schema` by `table_schema` and `table_name`; catalog-wide
  scans are slow.
- Only literal predicates prune reliably. On partition columns they skip
  partitions (for a transform such as `month(ts)`, use a literal range on `ts`);
  on `"$path"` they skip files. Subquery and join filters depend on dynamic
  filtering and often scan everything. Generate literal ranges or lists when
  pruning matters.
- `sorted_by` orders rows within each written file. Files written in parallel
  overlap in range, so it rarely lets Iceberg skip files. Partition stage
  tables on the column your batches filter.
- Make schema changes replay-safe with `add column if not exists` and
  `drop column if exists`. A failed schema commit (ICEBERG_COMMIT_ERROR) can
  leave the outcome unknown, so check the live schema before rerunning. Some
  metastores reject dropping a column that is not last; rebuild through a stage
  table instead.
- Memory limits such as EXCEEDED_GLOBAL_MEMORY_LIMIT apply per query, not per
  account. Split whole-table checks, such as `count(distinct ...)` or correlated
  `exists` scans, by partition.

## Long jobs

- Before launching a long job, state which batches (dates or partitions) it
  will process and an ETA from a measured rate: remaining batches × measured
  time per batch ÷ concurrency, scaled by batch size when sizes differ.
- Log per-batch progress with `time_print` and keep it in the job's log. Run
  background jobs with `python -u`, because `time_print` does not flush stdout.
  In every status update, give the ETA for the current stage and all remaining
  stages, with the measured rate.
- Give the job's SQL calls a stable `query_label`; only the first statement of
  a multi-statement string carries it.
- Before restarting a job, stop its process, then cancel its leftover queries:
  take the `sql.show_queries(db_key)` rows whose `query` text contains the
  label or the job's table names and pass their `query_id` values to
  `sql.cancel_queries(db_key, ids)`. Avoid `cancel_all=True`, which cancels all
  your active queries on that connection.
- A running batch call keeps its concurrency. To lower it, start no new batches
  until fewer than the new limit are running, or stop only the most recently
  started batches.
- On Trino, count other users' running queries with
  `sql.read("trino", '''select count(*) from system.runtime.queries where state in ('QUEUED', 'RUNNING') and "user" <> current_user''', output_type="scalar")`;
  `sql.show_queries` lists one user only.
- After fixing a failing step, smoke-test it on one batch before asking the
  user to relaunch the full run.
- In multi-table loads, publish each independent table as soon as its stage is
  complete.

## Storage per table

- When a pipeline or backfill keeps persistent or intermediate tables, give
  rows, size and files per table (for ClickHouse, active parts): estimates in
  every design option and measured values in every milestone report. Put them
  in chat or design notes, not in report outputs. Measure with metadata:
  - Trino Iceberg: `"<table>$partitions"` (`record_count`, `file_count`,
    `total_size`) or `"<table>$files"`. `sql.show_tables` returns no Trino row
    counts or sizes.
  - ClickHouse: `sql.show_tables(db_key, schema=..., table_name=..., ch_distributed_table_stats=True)`
    gives a Distributed table's rows and size. For parts, read
    `cluster('<cluster>', 'system', 'parts')` filtered to the local table and
    `active`.
  - Greenplum: `sql.show_tables` gives the size from `pg_total_relation_size`
    and an estimated row count. A partitioned parent's size excludes its child
    partitions, so sum the children.
- Persist the smallest state that enables pruning or incremental work (keys,
  hashes, link tables, dates). Do not persist full copies of large sources
  unless the user approves. Present the most compact variant first.
- When every batch would rescan a large lookup source, build a compact
  persistent lookup once (keys only, partitioned by date through
  `partition_by`; Greenplum also needs `gp_partitions`). Then process the fact
  table partition by partition against it.

## Sparse daily facts

- Store per-user daily activity facts sparsely, only rows with activity. Derive
  zeros at analysis time: left join the cohort to the facts and use
  `coalesce(..., 0)`, so every cohort user has one row before
  `ab.compute_test_metrics` or the SQL-native AB helpers. Those helpers ignore
  missing metric values instead of counting them as zero.
- Do not build zero-filled user × day grids unless asked. When asked, state the
  expected row count first and offer the sparse table plus a calendar derived
  at read time.

## SQL style

Defaults for new SQL. When you edit an existing file or follow a reference
project the user names, keep its style.

- Write keywords in lowercase. Write filters as `where 1=1` followed by
  `and ...` lines, and grouping as `group by 1, 2, 3` on one line.
- List columns explicitly: no `select *` or `t1.*`. Put one selected column per
  line.
- Alias tables `t1`, `t2`, `t3`, ... in order of appearance within each select.
  Restart the numbering in every subquery and `union` branch, and qualify
  columns with the aliases.
- Join with plain `=`. Use `is not distinct from` only for a key where NULL
  must match NULL, and say why.
- To deduplicate across branches, use `union all` in a CTE, then an outer
  `select distinct`.
- Label literal placeholders in insert/select lists with the target column
  (`0 as revenue`). Inserts still match columns by position.
- In DDL, declare column names and types explicitly. Load seed rows separately
  instead of `create table ... as select ... from (values ...)`.
- Greenplum temp tables: `drop table if exists pg_temp.<t>;`, then
  `create temporary table <t> as(` query `) distributed by (<key>);`, then
  `analyze <t>;`. Choose the key from the later joins or aggregations. Leave no
  blank lines inside a block and one blank line between blocks. A temp table
  lasts only as long as its connection, and each toolkit call or list item
  opens its own, so put the blocks and the statements that read them in one
  string passed to `sql.execute`, `sql.execute_read`, `sql.execute_insert` or
  `sql.execute_create`.

## SQL files and numbering

- Keep every query of an analytics script in a `.sql` file under `sql/`, with
  one `sql/<script>/` subfolder per script in multi-script projects. Load it
  with `read_file_here(path, params)`. Inline SQL in Python only for trivial
  metadata reads.
- `read_file_here` fills `params` with `str.format`, so double literal braces in
  files that take params (ClickHouse `'{{cluster}}'`, `{{shard}}`). It resolves
  paths from the folder of the launched script (in Positron, the editor file),
  so keep an entry-point script in the same folder as the scripts it runs.
- One file may hold several statements; run it with one `sql.execute` call.
- Prefix files with single-digit stage numbers in execution order, without zero
  padding (`0_ddl.sql`, `1_data_checks.sql`, `2_increment.sql`, not `00_`). Add
  related steps as decimals (`3.1_optimize.sql`, `3.2_cleanup.sql`). Put
  permanent-table DDL in stage 0, but keep Greenplum temp-table blocks in the
  file that reads them. Renumber files and their references after merging or
  inserting steps.
- When a ticket needs several scripts, add one top-level entry-point script
  that runs them in order.
