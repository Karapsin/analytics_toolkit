[SQL module index](index.md)

# Main Entry Points

## General Functions

- `read_sql` / `read`: run a query and return a dataframe
- `execute_sql` / `execute`: run SQL statements without returning a dataframe
- `execute_read`: run setup SQL statements and return the final statement as a
  dataframe
- `load_df`: load a pandas dataframe into a SQL table
- `transfer_table` / `transfer`: move data between supported backends
- `create_table_from_sql`: create a table from a source query's native column
  metadata, optionally inserting the query result
- `create_sql_table`: build and execute `CREATE TABLE` statements
- `table_info`: inspect live table existence, columns, optional row count, and
  resolved backend table names
- `show_tables`: list tables visible through backend metadata as `db`, `schema`,
  `table_name`, `row_count`, and human-readable `table_size`
- `drop_many_partitions`: remove multiple table partitions with backend-specific
  SQL
- `extract_ddl`: return native `CREATE TABLE` DDL for one table or a sequence
  of tables
- `format_plan`: render a compact multi-line summary for a `SqlPlan`
- `async_sql`: run a named batch of independent SQL tasks or custom pipelines
  concurrently through the existing sync APIs
- `parallel_sql`: run the same task specs as `async_sql` through synchronous
  thread-based parallelism
- `airflow_query_label`: build a sanitized SQL query label from Airflow context
  fields
- `get_sql_connection`: open a backend connection directly
- `with_sql_connection`: decorate a function with managed connection lifecycle

## Backend-Specific Functions

- `gp_create_many_partitions`: create Greenplum range or list partitions from
  explicit intervals, values, days, weeks, months, or years
- `build_gp_create_many_partitions_sqls`: render Greenplum partition DDL
  without opening a connection
- `gp_vacuum`: run Greenplum `VACUUM` outside a transaction block
- `gp_cancel_all_running_queries`: cancel current-user Greenplum backend
  queries, excluding the caller session
- `ch_create_table_as`: recreate a ClickHouse distributed/shard table pair
  from a query result
- `ch_drop_table`: drop a ClickHouse distributed table and its managed shard
  table
- `ch_full_table_move`: recreate a ClickHouse distributed/shard table pair
  from source DDL, copy all rows, and drop the source pair

Public type aliases are exported for common option bundles and annotations:
`BackendName`, `ConnectionKey`, `SqlText`, `TableName`, and `SqlTaskType`.

`read_sql`, `execute_sql`, `execute_read`, `load_df`, `transfer_table`, and
`drop_many_partitions` all support `retry_cnt` and `timeout_increment`. The
backend-specific `gp_create_many_partitions` helper supports the same retry
options. Retries restart the whole public operation from the beginning with a
fresh connection. Deterministic SQL errors such as syntax errors, invalid
grouping, missing tables/columns/functions/schemas, and
PostgreSQL/Greenplum undefined objects or unsupported feature errors are not
retried.

`load_df` and `transfer_table` can show `tqdm` row progress bars during data
loading. Progress bars are disabled by default; pass `progress=True` to enable
them. `dry_run=True` and `return_sql=True` return plans without creating
progress bars. Transfer row progress is indeterminate by default; pass
`estimate_total_rows=True` to use a
best-effort backend planner estimate as the progress total. `transfer_table`
formats progress row counts with underscore digit grouping, for example
`1_722_355row` or `1_722_355/2_000_000`.

`execute_sql` and `execute_read` split multi-statement SQL for Trino and
ClickHouse and submit each statement sequentially; the next statement is sent
only after the previous driver call returns. `execute_read` executes every
statement except the last, then reads the last statement into a pandas dataframe
on the same connection. Greenplum keeps its historical default of executing the
setup SQL as one statement set unless `gp_break_query=True` is passed. Pass
`progress=True` to enable multi-statement progress bars.

`show_tables(db_key, schema=None, conditions=None, table_name=None,
ch_distributed_table_stats=False)` returns a pandas dataframe with exactly
`db`, `schema`, `table_name`, `row_count`, and `table_size` columns. `row_count`
and `table_size` come from backend metadata when available; `table_size` is
formatted as a human-readable string. `schema` filters ClickHouse `database` or
SQL `table_schema`; `table_name` accepts one table name string or a sequence of
table name strings for exact matching; when `schema` is supplied, matching
`schema.table` values are accepted too. `conditions` is appended to the
metadata query as `AND (<conditions>)`, so backend-native predicates are
accepted.

Greenplum uses `information_schema.tables` plus PostgreSQL/Greenplum relation
metadata, and Trino uses `<catalog>.information_schema.tables`. Trino standard
table metadata does not expose portable row-count or table-size values, so
Trino rows return `None` in those columns. ClickHouse uses local
`system.tables` metadata by default. Pass `ch_distributed_table_stats=True` to
resolve `Distributed(...)` engine metadata and sum `total_rows` and
`total_bytes` from the logical shard tables through `cluster(...)`.

`extract_ddl(db_key, tables)` accepts one table name string or a sequence of
table name strings and returns the native DDL statements joined with newlines.
Each returned statement has exactly one trailing semicolon. Trino unqualified
table names are resolved from the connection's configured catalog and schema.

```python
ddl = sql.extract_ddl("trino", ["events", "mart.orders"])
```

`async_sql` and `parallel_sql` are synchronous public functions: call them
directly and they return a result dictionary. They accept a non-empty sequence
of task specs. Each spec declares a `type` (`read`, `execute`, `execute_read`,
`load_df`, `transfer`, or `custom_sql_pipeline`). SQL task specs pass the same
keyword arguments as the matching sync function. Add an optional `name` field to
control the result key; unnamed tasks are keyed as `task_0`, `task_1`, and so
on. `async_sql` runs blocking sync calls in worker threads, while
`parallel_sql` uses a `ThreadPoolExecutor`; neither function parallelizes an
individual SQL statement internally. Result keys follow the input task order.
With `fail_fast=True`, the
first raised task exception is raised and pending tasks are cancelled;
already-running sync work can continue until that function exits. Successful
task results are preserved, except `None` results are reported as `"success"`.
With `fail_fast=False`, failed tasks are reported under their task names as the
error text. A `tqdm` progress bar is disabled by default; pass `progress=True`
to enable a batch task bar. Built-in `execute`, `execute_read`, `load_df`, and
`transfer` tasks run with their inner progress bars suppressed inside batch
helpers, so the batch task bar is the only progress bar shown.

SQL query text is not printed by default. Pass `print_queries=True` to
`read_sql`, `execute_sql`, `execute_read`, or `gp_cancel_all_running_queries`
when you want each statement echoed before execution. `read_sql`,
`execute_sql`, and `execute_read` still log elapsed time after every executed
query or statement. SQL logs use structured tags for operation, connection,
backend, and phase, so message bodies avoid repeating those values. Public SQL
operations also log the first non-empty line of the representative SQL after the
operation-finished status line. Public functions exported from
`analytics_toolkit.sql` print a final `[timing]` function duration line,
including dry-run and `return_sql` paths.
Use `sql.set_time_print_sink("logging")` to route those messages through the
`analytics_toolkit` Python logger, which is usually a better fit for Airflow
task logs.
When a built-in SQL task fails inside `async_sql` or `parallel_sql`, the batch
helper prints the failed task name and its SQL field (`query` or `from_sql`) to
make concurrent task failures easier to diagnose.

Pass `start_comment` to prepend a raw SQL prefix to every `read`, `execute`,
`execute_read`, and `transfer` task query. For `transfer`, the prefix is applied
to `from_sql`. A task-level `start_comment` overrides the top-level default;
`None` or a blank string means no prefix for that task. The prefix is not
sanitized.

`soft_concurrency_cap` limits actual sync worker execution. When omitted, it
defaults to `concurrency`. `hard_concurrency_cap` defaults to `10` and rejects
calls only when actual possible worker execution after soft throttling would
exceed the hard cap. Lowering `soft_concurrency_cap` is therefore a valid way to
run a large requested batch without exceeding the hard cap.

```python
import pandas as pd
from analytics_toolkit import sql

tasks = [
    {
        "name": "users",
        "type": "read",
        "connection_type": "gp",
        "query": "select user_id, segment from sandbox.users",
    },
    {
        "name": "refresh_summary",
        "type": "execute",
        "connection_type": "gp",
        "query": "truncate table sandbox.summary",
        "gp_break_query": True,
    },
    {
        "name": "load_scores",
        "type": "load_df",
        "connection_type": "ch",
        "destination_table": "sandbox.scores",
        "df": pd.DataFrame({"user_id": [1], "score": [10]}),
        "append": False,
        "order_by": ["user_id"],
    },
    {
        "name": "copy_events",
        "type": "transfer",
        "from_db": "trino",
        "to_db": "gp_sandbox",
        "from_sql": "select * from iceberg.events.daily",
        "to_table": "sandbox.events_daily",
        "batch_size": 50_000,
        "start_comment": "/* copy_events async task */",
    },
]

result = sql.async_sql(
    tasks,
    concurrency=3,
    start_comment="/* nightly async batch */",
)

users_df = result["users"]
refresh_status = result["refresh_summary"]  # "success"
loaded_rows = result["load_scores"]
transferred_rows = result["copy_events"]
```

Use `custom_sql_pipeline` for ordered Python steps that should run sequentially
inside one task while other tasks continue under the outer concurrency limit.
Each step is called as `step(context)`. The context exposes `task_name`,
`step_index`, `results`, and `last_result`. Sync steps run in a worker thread;
async steps are awaited directly by `async_sql`. `parallel_sql` supports only
sync pipeline steps; use `async_sql` when a pipeline step is async or returns a
coroutine. A pipeline returns the final step result.

```python
def read_row_count(context):
    return sql.read(
        "gp",
        "select count(*) as row_count from sandbox.source_table",
    )


def transfer_if_not_empty(context):
    row_count = int(context.last_result["row_count"].iloc[0])
    if row_count == 0:
        return 0

    return sql.transfer(
        from_db="gp",
        to_db="ch",
        from_sql="select * from sandbox.source_table",
        to_table="sandbox.source_table_copy",
        order_by=["id"],
    )


result = sql.async_sql(
    [
        {
            "name": "source_copy",
            "type": "custom_sql_pipeline",
            "steps": [read_row_count, transfer_if_not_empty],
        }
    ],
    concurrency=3,
)
```

Pipeline steps can launch nested batches. The nested call below requests
two-way concurrency:

```python
def load_parts_in_parallel(context):
    return sql.async_sql(
        [
            {
                "name": "load_a",
                "type": "load_df",
                "connection_type": "gp",
                "destination_table": "sandbox.part_a",
                "df": df_a,
            },
            {
                "name": "load_b",
                "type": "load_df",
                "connection_type": "gp",
                "destination_table": "sandbox.part_b",
                "df": df_b,
            },
        ],
        concurrency=2,
    )


def finalize_parts(context):
    return sql.execute(
        "gp",
        """
        create table sandbox.final_parts as
        select * from sandbox.part_a
        union all
        select * from sandbox.part_b
        """,
    )
```

For a single large top-level batch, set an explicit soft cap below the hard cap:

```python
result = sql.async_sql(
    many_load_tasks,
    concurrency=20,
    soft_concurrency_cap=5,
    hard_concurrency_cap=10,
)
```

`transfer_table` streams source results as row batches. `batch_size` is the
initial fetch/insert size. By default `adaptive_batch_size=True` adjusts later
batches from successful insert latency: faster than half of
`target_batch_seconds` grows by 50%, slower than twice the target shrinks by
50%. `min_batch_size` and `max_batch_size` bound the adaptive size; when
`max_batch_size` is omitted in time-based mode it defaults to `batch_size * 4`.
Pass `target_batch_memory_mb` to adapt from the approximate in-process memory
used by each fetched row batch instead of insert latency. When it is set,
memory targeting wins over `target_batch_seconds`; batches smaller than half
the memory target grow by 50%, and batches larger than the target shrink
proportionally. If `max_batch_size` is omitted in memory mode, growth is
unlimited; pass `max_batch_size` to set an explicit hard ceiling.
Pass `estimate_total_rows=True` to ask the source backend for a non-executing
planner estimate before streaming batches. Greenplum and Trino use `EXPLAIN`
JSON output; ClickHouse uses `EXPLAIN ESTIMATE` only for simple single-table
`SELECT` statements. Estimates are approximate, can be unavailable, and never
add a `COUNT(*)` query.

For Greenplum targets, `load_df` accepts `gp_insert_chunk_size` to control the
`execute_values` page size for DataFrame inserts. The default is 10,000 rows per
statement so large DataFrames do not become one oversized `VALUES` statement;
lower it for very wide rows or tight VMEM quotas.

For Trino targets, `load_df` and `transfer_table` also accept
`trino_insert_chunk_size` to control how many rows are sent in each
parameterized multi-row insert statement. If omitted, the package falls back to
the target Trino connection's `insert_chunk_size`, then to the internal default.

Greenplum tables created by `create_sql_table`, `load_df`, `transfer_table`,
and `create_table_from_sql` default to append-only column-oriented storage:
`WITH (appendonly=true, blocksize=32768, compresstype=zstd, compresslevel=4,
orientation=column)`. Pass `gp_distributed_by_key` to use `DISTRIBUTED BY`;
otherwise created Greenplum tables use `DISTRIBUTED RANDOMLY`. Pass
`partition_by` to create a parent table with `PARTITION BY RANGE`; child
partitions are still created separately with `gp_create_many_partitions`.
`order_by` is not supported for Greenplum targets.

Trino tables created by these helpers use `WITH (format = 'PARQUET',
object_store_layout_enabled = true)`. Pass `partition_by` to add Iceberg
`partitioning = ARRAY[...]` and `order_by` to add `sorted_by = ARRAY[...]`.

[SQL module index](index.md)
