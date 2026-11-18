# SQL helpers

Facts about the public `sql` helpers, checked against analytics-toolkit
1.3.12.5. Installed builds can differ: check an argument this page omits with
`inspect.signature(sql.<name>)` and the
[function reference](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/sql/functions/index.md)
instead of reading toolkit source. Aliases in examples (`trino`, `ch`, `gp`)
are placeholders; list the configured ones with `sql.validate_connections()`.
Recipes and working rules live in [SQL practices](sql-practices.md).

## Signature card

Most public SQL wrappers have no docstrings, so `help()` shows little. Single-
database helpers take `db_key` first; `sql.transfer` takes `from_db, to_db`.
Pass the leading arguments shown without defaults positionally and everything
else by keyword: apart from `sql.read` and `sql.execute`, the card lists
selected arguments, not their positional order. The query and write helpers
also accept the keyword `ch_unavailable_shards=` for ClickHouse.

- `sql.read(db_key, query, print_queries=False, retry_cnt=5, timeout_increment=5, query_label=None, return_metadata=False, output_type="df", to_excel=None, to_csv=None, row_limit=None)`
  runs exactly one statement and returns a DataFrame. `output_type="scalar"`
  needs one row and one column, `"list"` one column, `"dict"` unique column
  names. `row_limit` must be a positive int; it caps the rows returned, not the
  rows the query reads. `to_csv` or `to_excel` (one of them, with
  `output_type="df"`) writes the frame without its index.
- `sql.execute(db_key, query, print_queries=False, gp_break_query=False, gp_commit_each_statement=False, retry_cnt=5, timeout_increment=5, query_label=None, dry_run=False, return_sql=False, return_metadata=False, progress=False, concurrency=1, soft_concurrency_cap=None, hard_concurrency_cap=5)`
  returns None; a list input returns a list in input order.
- `sql.execute_read(db_key, query, ..., row_limit=None)` takes the execution
  arguments of `sql.execute` (without `dry_run` and `return_sql`) and returns the
  final SELECT as a DataFrame.
- `sql.execute_insert(db_key, table_name, query, *, gp_break_query=False, retry_cnt=5, query_label=None, dry_run=False, return_metadata=False, concurrency=1, ...)`
  inserts the final SELECT into an existing table by column position and
  returns the row count.
- `sql.execute_create(db_key, table_name, query, *, drop_if_exists=False, if_not_exists=False, partition_by=None, order_by=None, gp_distributed_by_key=None, gp_partitions=None, ch_only_shard=False, ...)`
  creates a table from the final SELECT and returns the row count.
- `sql.insert(db_key, table_name, query, *, retry_cnt=5, query_label=None, dry_run=False, return_metadata=False, concurrency=1, ...)`
  inserts exactly one SELECT by column position and returns the row count.
  `table_name` and `query` may be matching lists.
- `sql.create_table(db_key, table_name, df=None, *, sql=None, source_db=None, insert_data=False, drop_if_exists=False, if_not_exists=False, partition_by=None, order_by=None, gp_distributed_by_key=None, gp_partitions=None, only_generate_sql=False, table_schema=None, ...)`
  takes exactly one of `df`, `sql`, `table_schema`. With `df=` it only creates
  an empty table from the dtypes; `insert_data=True` applies only to `sql=`.
- `sql.load_df(db_key, destination_table, df, append=False, write_mode=None, key_columns=None, partition_by=None, order_by=None, table_schema=None, empty_source_policy=None, ...)`.
- `sql.transfer(from_db, to_db, from_sql=None, to_table=None, from_table=None, write_mode="replace", key_columns=None, transfer_keys=None, transfer_key_values=None, concurrency=None, full_retry_cnt=5, full_timeout_increment=600, ignore_source_staging=False, trino_mode=None, validate_row_count=True, empty_source_policy=None, ...)`.
- `sql.extract_ddl(db_key, tables)`,
  `sql.table_info(db_key, table, include_row_count=False)`,
  `sql.show_tables(db_key, schema=None, conditions=None, table_name=None, ch_distributed_table_stats=False, trino_catalog=None)`:
  see [metadata helpers](#metadata-and-maintenance-helpers).
- `sql.drop_tables(db_key, table, *, if_exists=False, ...)`,
  `sql.drop_partitions(db_key, table, partition_keys_list, trino_partition_column=None, gp_truncate=False, *, ...)`,
  `sql.gp_create_partitions(db_key, table, *, days=None, weeks=None, months=None, years=None, values=None, intervals=None, name_template="p_{}", ...)`.
- `sql.show_queries(db_key, *, user=None, state="active", ...)`,
  `sql.cancel_queries(db_key, query_ids=None, *, cancel_all=False, ...)`.
- `sql.async_sql(tasks, *, concurrency=5, fail_fast=True, ...)` and
  `sql.parallel_sql(tasks, *, concurrency=5, fail_fast=True, ...)`.
- `sql.validate_connections(keys=None, *, connect=False)`.

Return options of the read and write helpers:

- `return_metadata=True` returns `SqlOperationResult` with `.affected_rows` and
  `.inserted_rows` (one result per item for list input). Log written row counts
  from it instead of running extra count queries.
- `dry_run=True` or `return_sql=True` returns an `SqlPlan` (`.sqls`) without
  running anything; `sql.format_plan(plan)` renders it as text.

`sql.__all__` lists the public API. There is no `sql.table_exists`, `sql.load`
or `sql.read_sql`. `[read_sql]` and `[execute_sql]` in log lines are the
internal names of `sql.read` and `sql.execute`.

## Statements per call

- `sql.read` runs exactly one statement (trailing comments are fine).
- `sql.execute(db_key, sql_text)` accepts several `;`-separated statements in
  one string and splits them itself. Do not split on `;` yourself.
  - Trino runs them in order on one connection, so `set session` and `use`
    carry over.
  - ClickHouse runs them in order, but the HTTP driver sends each statement as
    its own request without a session, so `SET` and temporary tables do not
    carry over. Put settings in the statement's `SETTINGS` clause and use
    regular stage tables. The toolkit rejects inline `skip_unavailable_shards`;
    use `ch_unavailable_shards=` instead.
  - Greenplum sends the whole string at once in one transaction, so temp tables
    and `set` carry over and an error rolls everything back.
    `gp_break_query=True` runs the statements one by one, still in one
    transaction unless `gp_commit_each_statement=True`.
- `sql.execute_read`, `sql.execute_insert` and `sql.execute_create` run the
  setup statements and the final SELECT on one connection. `sql.insert`
  accepts exactly one SELECT.
- A list (`sql.execute(db_key, [script_1, script_2])`, and the same for
  `execute_read`, `execute_insert`, `execute_create`, and `insert` with one
  SELECT per item) runs independent items, each on its own connection with its
  own retries. Items share no session state or temp tables. `concurrency`
  defaults to 1, so items run in order; a value above `hard_concurrency_cap`
  (default 5) raises instead of being capped. A failure cancels the items not
  yet started and raises `sql.SqlBatchExecutionError`. Use its
  `successful_indexes`, `failed_indexes`, `safe_to_retry_indexes` or
  `safe_to_retry_queries` (failed and not-started items) instead of parsing the
  message. A failed item may already have run some of its statements.
- `sql.execute(db_key, sql_text, dry_run=True).sqls` shows how the string will
  be split, without connecting. Greenplum without `gp_break_query` returns one
  item.

## Retries

- `retry_cnt` counts toolkit attempts: default 5, minimum 1 (`0` raises
  ValueError). After failed attempt n the toolkit waits n × `timeout_increment`
  seconds (default 5, so 5, 10, 15 and 20 s).
- Recognized permanent errors (syntax, missing table or column, permission,
  authentication) stop at once and log "Failed with a non-retryable error".
  "Failed on attempt n/N" means another attempt follows. Explicit
  unknown-outcome errors also stop; the
  [retry policy](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/sql/retry-policy.md)
  lists them per backend.
- Other failures rerun the whole call on a fresh connection, including writes
  that may already have committed. A multi-statement string restarts from its
  first statement:
  - Trino: statements that already finished run again; each statement commits
    on its own.
  - Greenplum: the failed transaction was rolled back, so the rerun starts clean
    unless the commit itself was lost. With `gp_commit_each_statement=True`,
    committed statements run again.
  - ClickHouse: once a write (INSERT, CREATE ... AS SELECT, DROP, TRUNCATE,
    ALTER and similar) has been submitted to a table the toolkit does not own,
    a later failure stops automatic retries for the whole call, including
    multi-statement scripts. The call raises. Check what finished before
    rerunning; a manual rerun replays the script from its first statement.
  - List items retry independently, and completed items are never replayed.
- `AmbiguousSqlReplaceError` means a replace (`transfer`, `load_df`, other
  replace writes, or destructive ClickHouse DDL after a live write in the same
  call) could not prove the final table state. It is not retried. The server
  error is chained as `__cause__`. Inspect the target, and any preserved
  recovery tables, before rerunning.
- Before rerunning a failed write, check the target with
  `sql.table_info(db_key, table, include_row_count=True)` or a
  partition-filtered count. `sql.show_queries(db_key, state="all")` shows recent
  Trino and ClickHouse queries; on Greenplum it shows only active sessions.
- `retry_policy` was removed. Passing it raises TypeError, so delete it from old
  scripts.

## Metadata and maintenance helpers

| Need | Helper |
| --- | --- |
| List tables | `sql.show_tables(db_key, schema=..., table_name=..., conditions="table_name like '%x%'")`. `row_count` and `table_size` come from catalog metadata: empty on Trino, estimates on Greenplum; ClickHouse Distributed tables need `ch_distributed_table_stats=True` |
| Existence, columns, row count | `sql.table_info(db_key, table)` → `.exists`, `.columns` (name → type), `.row_count` (runs `count(*)`, only with `include_row_count=True`), `.shard_table` (on ClickHouse, `<table>_shard` by naming convention only) |
| Native DDL with partitioning | `sql.extract_ddl(db_key, tables)` (a name or a list). Trino Iceberg shows `partitioning = ARRAY[...]`, but `SHOW CREATE TABLE` can be denied in shared schemas and fails on views. Greenplum shows the partition clause and `DISTRIBUTED BY`. A ClickHouse Distributed table shows only its `Distributed('<cluster>', '<db>', '<local table>', ...)` engine |
| Partition keys and values | No helper; use `sql.read` on metadata, see [partition discovery](sql-practices.md#partition-discovery) |
| Configured aliases | `sql.validate_connections(keys=None, connect=False)` → results with `.connection_key`, `.backend`, `.valid`, `.error` |
| Drop tables | `sql.drop_tables(db_key, [t1, t2], if_exists=True)`; on ClickHouse it also drops `<table>_shard` on the cluster |
| Drop or truncate partitions | `sql.drop_partitions(db_key, table, ["2026-10-01"])`; preview with `dry_run=True`. Greenplum: `ALTER TABLE ... DROP PARTITION FOR (...)`, or `TRUNCATE PARTITION` with `gp_truncate=True`. Trino: needs `trino_partition_column=` and deletes `DATE` values only. ClickHouse: `ALTER TABLE <table>_shard ON CLUSTER ... DROP PARTITION`, so it works only for toolkit-style table pairs |
| Create Greenplum range or list partitions | `sql.gp_create_partitions(db_key, table, months=["2026-10-01", "2026-11-01"])` (or `days`, `weeks`, `years`, `values`, `intervals`) |
| Other maintenance | `sql.gp_vacuum`, `sql.gp_analyze_partitioned_table`, `sql.ch_reconfigure_table`, `sql.cleanup_stale_stage_tables` |

## Running queries

- Pass `query_label="<job>"` to every call of a long job. The toolkit prefixes
  each statement with `/* analytics_toolkit query_label=<job> */`; characters
  other than letters, digits, spaces and `_.:=,@/+-` become `_`. `sql.execute`
  labels a multi-statement script once, so on Trino and ClickHouse only the
  script's first statement carries the label.
- `sql.show_queries(db_key)` lists the connection user's active queries with
  `query_id` (a PID on Greenplum), `state`, `query`, `elapsed_seconds`,
  `raw_state` and more. Trino rows have no timings; read `created` and `started`
  from `system.runtime.queries` for an ETA. `state="finished"`, `"failed"` or
  `"all"` works on Trino and ClickHouse only and returns no error text; read
  `exception` from ClickHouse `system.query_log` or `error_code` from Trino
  `system.runtime.queries`. `user="<account>"` shows another account's queries
  when the connection may see them.
- Neither helper filters by label. Select a job's rows with
  `job = running.query("query.str.contains('query_label=<job>', regex=False, na=False)", engine="python")`
  and stop them with `sql.cancel_queries(db_key, job["query_id"].tolist())`;
  an empty list raises. `cancel_all=True` cancels every active query of the
  connection user, unrelated ones included. On Greenplum, cancellation also
  terminates the session.
- On ClickHouse both helpers see only the connected host unless the alias uses
  `cluster_routing`. For other hosts, read
  `clusterAllReplicas('<cluster>', 'system', 'processes')` with `sql.read`.
- Stopping the Python process does not reliably stop server-side queries. Only
  `sql.async_sql` cancels its batch's running queries on Ctrl+C. Do not use
  private driver objects, REST calls, manual kill loops or browser UIs.

## Concurrency

- `sql.async_sql(tasks)` and `sql.parallel_sql(tasks)` (defaults
  `concurrency=5`, `fail_fast=True`) run independent queries or pipelines
  concurrently. Task spec:
  `{"name": "visits", "type": "read", "db_key": "trino", "query": ...}`. `type`
  is `read`, `execute`, `execute_read`, `load_df` or `transfer` (with `from_db`
  and `to_db`); the other keys are that helper's arguments. For ordered Python
  steps use `{"name": "po", "type": "custom_sql_pipeline", "steps": [step_1, step_2]}`;
  each step is called with a context whose `last_result` is the previous step's
  result. The batch returns a dict keyed by task name; a task that returns None
  maps to `"success"`.
- With `fail_fast=True`, the first failure cancels tasks not yet started and
  raises after the running ones finish. `fail_fast=False` raises nothing, and a
  failed task's value is its error message, so check every value. Use it only
  when a failing task must not stop the others.
- On Ctrl+C, `sql.async_sql` cancels its running queries; `sql.parallel_sql`
  waits for them to finish.
- For many independent statements of one kind, pass a list to `sql.execute`,
  `sql.insert`, `sql.execute_insert`, `sql.execute_read` or
  `sql.execute_create`. Concurrency multiplies when a task runs its own list
  with `concurrency`.
- Do not wrap `sql.read` in `ThreadPoolExecutor`, subprocess pools or custom
  schedulers.

## Data movement

- DataFrame to table: `sql.load_df(db_key, table, df)` replaces the target by
  default. Otherwise pass `write_mode="append"`, `"truncate_insert"` or
  `"upsert"` (which needs `key_columns`), and use `table_schema=` to force types.
- Query result to a table on another alias:
  `sql.transfer(from_db, to_db, from_sql=..., to_table=...)`. `write_mode`
  defaults to `"replace"`; pass `"append"` for incremental loads. When one Trino
  query can read both sides, prefer an `insert ... select` inside Trino (see
  [moving rows between databases](sql-practices.md#moving-rows-between-databases)).
- To split a big source, put `{event_date}` where a filter condition belongs
  (`where {event_date}` or `and {event_date}`) and pass
  `transfer_keys="event_date", transfer_key_values=dates, concurrency=N`, with N
  at most `hard_concurrency_cap` (5 by default). Each slice replaces the
  placeholder with `(event_date) = <value>`, so the key must name a source
  column. For an expression, use `transfer_keys={"day": "<source SQL expression>"}`
  with `{day}` in `from_sql`. Inside an f-string, write `{{event_date}}`. With
  `from_table=`, no placeholder is needed. Do not end `from_sql` with `;`.
- For every `write_mode` except `"append"`, `sql.transfer` makes up to
  `full_retry_cnt=5` whole-transfer attempts and waits
  `attempt × full_timeout_increment` seconds between them (10, 20, 30, then 40
  minutes by default). Retry log lines may show only the exception type; the
  exception raised at the end carries the full message.
- Staging and edge cases:
  - `trino_mode=None` uses Parquet staging when the Trino target has the
    complete `s3_transfer_staging_schema`/`s3_transfer_staging_location` pair,
    otherwise `INSERT ... VALUES`. `"parquet"` or `"values"` forces one path.
  - `ignore_source_staging=True` streams directly instead of materializing in
    the source's `transfer_staging_schema`; target-side staging is unchanged.
  - `empty_source_policy` defaults to `replace` for replace and truncate modes,
    so a zero-row source leaves an empty target, and to `keep` for append and
    upsert. Pass `"keep"` or `"error"` for per-slice loads.
  - ClickHouse targets: `ch_sharding_key=` overrides the Distributed sharding
    expression, and `ch_only_shard=True` builds only the local table.
  - Replacing an existing replicated ClickHouse target on a cluster (the
    default `write_mode="replace"`) runs `SYSTEM SYNC REPLICA`. If the account
    lacks that privilege, load existing tables with `"truncate_insert"` or
    `"append"`.
- Known pitfalls:
  - Parquet staging into Trino declares naive timestamp columns as
    `TIMESTAMP(6)`. A staging catalog configured for milliseconds rejects them
    ("Incorrect timestamp precision"). Convert such columns to varchar or date
    in `from_sql` or the DataFrame and cast them back in SQL, or pass
    `trino_mode="values"`.
  - ClickHouse UInt64 values above the Int64 range break Parquet-staged
    transfers into Trino (`OverflowError`). Cast them to String in the source
    SQL.

## Connections

- On first use, `.connections` is searched upward from the running script's
  folder (or the editor file in Positron), then upward from the working
  directory. A file set with `set_connections_path(...)` is searched first. The
  first file parsed is cached for the process, and `.secrets` and relative
  certificate paths resolve beside it. Scripts saved in temporary folders
  (`/tmp`, `/var/folders`) and code run through `python -c` or stdin use only
  the working directory, so run such code from inside the project.
- Airflow connection IDs used by deployed DAGs may resolve only inside Airflow.
  Use the local aliases for local checks, and keep the DAG's IDs in DAG code.
- List aliases with
  `[(r.connection_key, r.backend) for r in sql.validate_connections()]`. An
  unknown-key error also names the available keys and the file in use.
- Each alias carries settings that the helpers apply automatically:
  `ddl_defaults` (table properties, ClickHouse engine and `ON CLUSTER` for
  regular and staging tables), transfer staging schemas, the ClickHouse outage
  default `ch_unavailable_shards`, query settings and timeouts. Do not open the
  file to learn them. To see the DDL a helper would run with them, render it
  without executing: `sql.execute_create(db_key, table, query, return_sql=True)`,
  `sql.create_table(..., only_generate_sql=True)` or
  `sql.load_df(..., return_sql=True)`.
- Per-call keywords override only some settings, such as
  `ch_unavailable_shards=`, `ignore_source_staging=`, `trino_mode=`,
  `ch_sharding_key=`, `ch_only_shard=`, `partition_by=` and `order_by=`. When a
  connection setting itself must change, name the key from the
  [configuration reference](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/sql/configuration.md)
  to the user and wait. Never build a temporary copy of the file.

## Result types

- `sql.read` and `sql.execute_read` infer dtypes from the returned values:
  `Int64`/`UInt64`, `Float64`, `boolean` and `string`. Timestamps become
  `datetime64[ns]`. Dates (`datetime.date`), decimals, nested values and empty,
  all-null or mixed columns stay `object`.
- A comparison involving `<NA>` returns `<NA>`, so `np.where(...)` or `if` on it
  raises "boolean value of NA is ambiguous". Apply `.fillna(False)` to the mask
  first.
- Comparing a date column with a string or a `datetime` raises TypeError.
  Compare it with `datetime.date` values, or convert it with
  `pd.to_datetime(df[col])`. `dt` helpers return ISO strings by default.
- Trino `timestamp with time zone`, Greenplum `timestamptz` and ClickHouse
  `DateTime` with a non-UTC timezone arrive timezone-aware. Excel export
  (`excel.*` and `to_excel=`) rejects them, so apply `.dt.tz_localize(None)`
  first.
- `@` references in `DataFrame.query` allow attributes and indexing but not
  slices or function calls (`@ids[:10]`, `@len(ids)`). Bind such values to a
  local variable first.
- Some environments (seen with pandas 2.3 on Python 3.14 with a source-built
  numpy 1.26) return `<NA>` for every row when a `string` column of 262,144 or
  more rows is compared with `==`, `!=` or `.eq()`. Masks and multi-condition
  `query` calls then silently keep 0 rows. Cast such columns with
  `.astype(object)` before comparing, and check row counts after filtering.
- With the HTTP ClickHouse driver, a query that returns no rows yields a
  DataFrame without columns: `output_type="dict"` gives `{}` and `"list"`
  raises. Check `df.empty`, or use `df.reindex(columns=[...])` before selecting
  columns.
