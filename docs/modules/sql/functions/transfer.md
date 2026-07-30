[SQL functions index](index.md)

# transfer

Stream data from a source SQL query into a target table on another configured connection.

```python
transfer(from_db: 'str', to_db: 'str', from_sql: 'str | None' = None, to_table: 'str | None' = None, from_table: 'str | None' = None, write_mode: 'str | None' = 'append', batch_size: 'int' = 100000, adaptive_batch_size: 'bool' = True, min_batch_size: 'int' = 1000, max_batch_size: 'int | None' = None, adaptive_batch_size_step: 'float' = 0.1, target_rows_per_second: 'bool' = True, target_batch_seconds: 'float | None' = None, min_batch_seconds: 'float | None' = None, max_batch_seconds: 'float | None' = None, target_batch_memory_mb: 'float | None' = None, min_batch_memory_mb: 'float | None' = None, max_batch_memory_mb: 'float | None' = None, target_rows_per_second_window: 'int' = 5, target_rows_per_second_deadband: 'float' = 0.15, retry_cnt: 'int' = 5, timeout_increment: 'float' = 5, full_retry_cnt: 'int' = 5, full_timeout_increment: 'float' = 600, key_columns: 'str | Sequence[str] | None' = None, upsert_partition_column: 'str | None' = None, gp_distributed_by_key: 'str | Sequence[str] | None' = None, gp_partitions: 'Mapping[str, Any] | None' = None, gp_insert_chunk_size: 'int | None' = None, trino_insert_chunk_size: 'int | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str | None' = None, ch_cluster: 'str | None' = None, ch_sharding_key: 'str | None' = None, ch_distributed_table: 'bool | None' = None, ch_distributed_engine_template: 'str | None' = None, ch_distributed_cluster: 'str | None' = None, ch_shard_on_cluster: 'str | None' = None, ch_distributed_on_cluster: 'str | None' = None, ch_only_shard: 'bool' = False, ch_retry_per_host_drops: 'bool' = True, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None, progress: 'bool' = False, estimate_total_rows: 'bool' = False, table_schema: 'dict[str, str] | None' = None, transfer_keys: 'str | Sequence[str] | Mapping[str, str] | None' = None, transfer_key_values: 'Sequence[Any] | Mapping[str, Sequence[Any]] | None' = None, concurrency: 'int | None' = None, read_concurrency: 'int | None' = None, write_concurrency: 'int | None' = None, ignore_source_staging: 'bool' = False, trino_mode: 'TrinoTransferMode | None' = None, validate_row_count: 'bool' = True, ch_count_limit_read: 'bool' = True) -> 'int | SqlPlan | SqlOperationResult'
```

## Inputs

### General Inputs

- `from_db` - source connection key or alias
- `to_db` - target connection key or alias
- `from_sql` - source SQL query used by a transfer; provide exactly one of `from_sql` or `from_table`
- `from_table` - source table name for simple `SELECT * FROM <from_table>` transfers; provide exactly one of `from_sql` or `from_table`
- `to_table` - target table name
- `write_mode` - write behavior: append (default), replace, truncate_insert, or upsert; `None` also resolves to append
- `batch_size` - initial number of rows fetched and inserted per transfer batch
- `adaptive_batch_size` - whether transfer batch size should adapt after successful inserts
- `min_batch_size` - minimum adaptive transfer batch size
- `max_batch_size` - maximum adaptive transfer batch size; `None` leaves the default behavior in place
- `adaptive_batch_size_step` - relative rows-per-second probe step used to shrink or grow adaptive row counts
- `target_rows_per_second` - optimize batch size by transfer throughput (`rows / second`) when `True`
- `target_rows_per_second_window` - number of recent successful throughput samples to average for throughput adaptation decisions
- `target_rows_per_second_deadband` - minimum relative throughput change needed to trigger throughput adaptation
- `target_batch_seconds` - target insert duration used by time-based adaptive batching
- `min_batch_seconds` - minimum allowed value for time-based adaptive targets when enabled
- `max_batch_seconds` - maximum allowed value for time-based adaptive targets when enabled
- `target_batch_memory_mb` - approximate in-process memory target used for adaptive transfer batches
- `min_batch_memory_mb` - minimum allowed value for memory-based adaptive targets when enabled
- `max_batch_memory_mb` - maximum allowed value for memory-based adaptive targets when enabled
- `key_columns` - key column or columns used to validate staged rows and required when `write_mode="upsert"`
- `upsert_partition_column` - single staged column that defines affected partitions for Trino and ClickHouse upsert replacement; required for those backends when `write_mode="upsert"`
- `retry_cnt` - number of operation retries with fresh connections
- `timeout_increment` - delay increment used between operation retries
- `full_retry_cnt` - number of retries for the whole transfer flow after a transfer-level failure
- `full_timeout_increment` - delay increment used between full transfer retries
- `dry_run` - when `True`, return a plan without mutating the database
- `return_sql` - when `True`, return a `SqlPlan` instead of mutating a database
- `return_metadata` - when `True`, return `SqlOperationResult` instead of the historical bare value
- `query_label` - safe label added to generated SQL comments, plans, metadata, and logs
- `progress` - whether to show progress bars for supported multi-step or row-loading operations
- `estimate_total_rows` - whether transfer should ask the source backend for a best-effort row estimate for progress
- `validate_row_count` - whether transfer should count source rows and validate source, streamed, and stage-table row counts before target finalization
- `ch_count_limit_read` - for ClickHouse sources, whether transfer should add a positive count-derived `LIMIT` to unbounded streamed reads during row-count validation; zero-count reads keep the original SQL
- `table_schema` - explicit backend-native column type mapping for created tables
- `transfer_keys` - optional placeholder name, placeholder-name sequence, or `{placeholder_name: sql_expression}` mapping used to split the source query into explicit keyed slices
- `transfer_key_values` - explicit values to transfer for `transfer_keys`; a single key accepts a sequence or `{placeholder_name: values}`, while multiple keys require `{placeholder_name: values}` for every key
- `concurrency` - legacy combined reader/writer count; omission resolves to one reader and one writer, it cannot be combined with either split setting, and unkeyed source-staged transfers cap workers at `ceil(total_rows / batch_size)`
- `read_concurrency` - source-reader count for keyed transfers; defaults to `1` when omitted and is capped by the slice count
- `write_concurrency` - target-stage writer count for keyed transfers; defaults to `1` when omitted and is capped by the slice count
- `ignore_source_staging` - when `True`, ignore the source connection's `transfer_staging_schema` for this call and use direct streaming; target-side staging is unchanged
- `partition_by` - partitioning columns or expression for created tables, interpreted according to the target backend
- `order_by` - ordering or sorting columns or expression for created tables, interpreted according to the target backend

### Backend-Specific Inputs

- `gp_distributed_by_key` - distribution key column or columns for created Greenplum target tables
- `gp_partitions` - initial Greenplum range or list child definitions used when the final target is created
- `gp_insert_chunk_size` - initial Greenplum transfer stage insert page size; omitted values start at `10_000`
- `trino_insert_chunk_size` - number of rows per Trino parameterized multi-row insert statement
- `trino_mode` - Trino target staging mode: `None` keeps automatic selection, `"parquet"` forces object-storage Parquet staging, and `"values"` forces generated multi-row `INSERT ... VALUES`
- `ch_engine` - nullable override for the configured ClickHouse shard engine
- `ch_cluster` - deprecated compatibility shortcut that fills both execution clusters and the routing cluster; use the dedicated fields below
- `ch_sharding_key` - nullable override for the configured Distributed sharding expression
- `ch_distributed_table` - nullable topology override; true creates a shard/Distributed pair and false creates one physical table
- `ch_distributed_engine_template` - nullable override for the configured `Distributed(...)` engine template
- `ch_distributed_cluster` - nullable override for the routing cluster used as the first `Distributed(...)` argument
- `ch_shard_on_cluster` - nullable override for the shard/physical-table `ON CLUSTER` target
- `ch_distributed_on_cluster` - nullable override for the Distributed facade's independent `ON CLUSTER` target
- `ch_only_shard` - for ClickHouse, create or mutate only the local table instead of a distributed/shard pair
- `ch_retry_per_host_drops` - whether ClickHouse replace/drop flows may retry direct local drops on affected hosts

## Usage

```python
from analytics_toolkit import sql

rows = sql.transfer(
    from_db="trino",
    to_db="gp",
    from_table="sandbox.orders",
    to_table="sandbox.orders_copy",
    batch_size=50_000,
)
```

Output example:

```python
rows
# 125000
```

Omitting `write_mode` appends rows. Pass `write_mode="replace"` explicitly for
full-table refreshes. A zero-row source leaves an existing target unchanged,
including in replace mode.

Single-key slices require one or more predicate placeholder occurrences in
`from_sql`. Every occurrence is replaced with the full predicate, not just the
literal value:

```python
from analytics_toolkit import sql

rows = sql.transfer(
    from_db="ch",
    to_db="trino",
    from_sql="""
        select event_date as dt, store_id, product_id
        from sandbox.loyalty_events
        where event_name in ('purchase', 'return')
          and {event_date}
    """,
    to_table="iceberg.sandbox.loyalty_events_copy",
    transfer_keys="event_date",
    transfer_key_values=["2026-04-01", "2026-04-02"],
    concurrency=4,
)
```

For direct keyed transfers, source reads and target staging can be tuned
independently. Each source slice belongs to one reader; writers consume the
bounded queue into private stage tables, and the target is finalized only after
every slice and stage has been validated:

```python
rows = sql.transfer(
    from_db="ch",
    to_db="gp",
    from_table="sandbox.events",
    to_table="sandbox.events_copy",
    transfer_keys="event_date",
    transfer_key_values=["2026-05-18", "2026-05-19", "2026-05-20"],
    read_concurrency=6,
    write_concurrency=2,
)
```

When the source connection defines `transfer_staging_schema`, keyed transfers
use a strict two-phase flow. Reader workers first materialize their assigned
keys into one private source-side stage each. After every reader finishes,
target writers dynamically claim whole keys, stream them through RAM batches,
and write private target-side stages. A key is never split between writers.
Independent readers may observe slightly different source moments.

Set `ignore_source_staging=True` to bypass the configured source schema for one
call and use the direct bounded-queue pipeline instead.

For simple table sources, use `from_table`; keyed table transfers do not need
placeholders because the helper generates the `WHERE` clause:

```python
from analytics_toolkit import sql

rows = sql.transfer(
    from_db="ch",
    to_db="trino",
    from_table="dm_nrt.loyalty_events",
    to_table="iceberg.sandbox.loyalty_events_copy",
    transfer_keys="event_date",
    transfer_key_values=["2026-04-01", "2026-04-02"],
    concurrency=4,
)
```

Expression keys use mapping form. Mapping keys are placeholder names, and
mapping values are trusted SQL expressions:

```python
from analytics_toolkit import sql

rows = sql.transfer(
    from_db="trino",
    to_db="gp",
    from_sql="""
        select user_id, event_date, amount
        from events
        where {event_date}
          and {user_id_suffix}
    """,
    to_table="sandbox.events_copy",
    transfer_keys={
        "event_date": "event_date",
        "user_id_suffix": "right(user_id, 1)",
    },
    transfer_key_values={
        "event_date": ["2026-04-01"],
        "user_id_suffix": ["0", "1", "2"],
    },
    concurrency=3,
)
```

## Notes

- Every real call generates one immutable UUID4 transfer ID. The same full ID
  appears in source snapshots, worker stages, Parquet resources, query labels,
  progress/log messages, errors, and `SqlOperationResult.metadata.transfer_id`.
  Dry-run plans use `<runtime-transfer-id>` and create no resources.
- Workers created by one call are supported and share that transfer ID. Two
  independent transfers targeting the same exact destination at the same time
  are unsupported. Startup cleanup is best effort, not a distributed lock; an
  older process can still execute SQL and overlapping finalization has no
  portable cross-backend fencing or atomic “new transfer wins” guarantee.
- On direct keyed transfers, readers and target writers use independent
  connections. The queue holds at most one prefetched batch per effective
  writer, and each SQL writer owns one private stage table. Successful batches
  may move between slices and writers while immutable slice IDs and row
  ordinals preserve validation. Stage rows remain until the single final target
  operation succeeds or attempt cleanup runs.
- Split concurrency is limited to keyed transfers. Legacy `concurrency=N`
  retains combined `N/N` behavior; unkeyed source-staged transfers retain their
  existing single-snapshot implementation.
- A configured source `transfer_staging_schema` creates one immutable source
  stage per effective reader for keyed transfers. Each key belongs to exactly
  one source reader and one target writer, and target writing begins only after
  all source stages finish. Unkeyed transfers continue to use slice zero in one
  source snapshot with bounded ordinal ranges.
- Unkeyed source-staged transfers keep at least one worker and use no more than
  `min(concurrency, ceil(total_rows / batch_size))` workers. Logs report both
  requested and effective counts. Before multiple non-upsert worker stages are
  consolidated, the coordinator opens a fresh target connection so idle
  expiry during worker loading cannot break the merge.
- Internal snapshot, source-batch, stage-identity, ordinal, and superseded-stage
  queries use distinct info-level action and phase labels in transfer logs.
- Transfer SQL stage names begin with a stable 16-hex destination hash and also
  carry the full transfer ID and worker/role identity. The hash is a naming
  prefix, not deletion authority. Exact canonical destination values stored in
  stage rows control validation and automatic cleanup; collisions allocate a
  different stage name instead of reusing, overwriting, or dropping a table.
- Four collision-resolved generated columns carry transfer ID, exact canonical
  destination, slice ID, and row ordinal through staging. They are mandatory
  integrity metadata even with `validate_row_count=False` and are explicitly
  excluded from final-target DDL and inserts.
- No manifest, lease, owner marker, heartbeat, bookkeeping, or other persistent
  coordination table is created. In-process checkpoints exist only for the
  current call.
- Prefer this short entrypoint in user-facing examples.
- Retries restart the public operation with fresh connections.
- Deterministic input and configuration errors stop immediately without an
  operation or full-transfer retry. This includes missing Trino `catalog` or
  `schema` settings required to resolve qualified target table names and
  ClickHouse server-side parsing, unknown-identifier, or type-conversion
  failures. Client-side value-adaptation failures also stop immediately.
  ClickHouse transport and incomplete-stream failures remain retryable when
  they do not include a deterministic conversion diagnostic.
- Greenplum stages normalize native Python UUID values to driver-safe canonical
  strings while preserving the destination column's `UUID` type.
- Row counts, chunk sizes, retry counts, adaptation windows, and concurrency
  must be built-in positive integers. Retry delays and adaptation rates must be
  finite real numbers with the documented positive or non-negative bounds.
  Validation also runs for dry-run and returned-plan calls.
- For GP and Trino targets that do not exist, transfer creates the empty final
  target before staging rows so missing target schemas or create permissions
  fail before expensive batch work. If the transfer later fails, that target is
  dropped only when it did not exist at the start of the transfer. ClickHouse
  target creation stays in finalization so row-based nullability refinement is
  preserved.
- When the source returns zero rows and the target table does not exist,
  transfer emits a warning, creates no target table, and returns `0`.
- Provide exactly one source input: `from_sql` for custom queries or
  `from_table` for simple table copies. Passing both raises
  `Provide only one of from_sql or from_table.` Passing neither raises
  `Provide exactly one of from_sql or from_table.`
- `transfer_keys` string and list forms accept only simple placeholder names
  such as `"event_date"`. Use mapping form for SQL expressions, for example
  `{"user_id_suffix": "right(user_id, 1)"}`.
- Each keyed transfer requires at least one `{placeholder_name}` occurrence in
  `from_sql` for every transfer key. The same placeholder may appear multiple
  times, and every occurrence receives the active slice predicate. `from_table`
  keyed transfers do not need placeholders. If `from_sql` is an f-string,
  escape braces as `{{event_date}}`.
- Key placeholders are replaced by predicates such as
  `(event_date) = DATE '2026-04-01'` or `(event_date) IS NULL`; they are not
  value-only placeholders.
- Values are always explicit; the helper does not query distinct key values
  automatically.
- Keyed transfers render each source slice by replacing placeholders inline.
  With source staging, every slice is materialized incrementally into one
  snapshot before range workers start. On the compatibility path without source
  staging, workers stream assigned slices into private target stages as before.
  Worker stages are consolidated before one final target write.
  Trino Parquet staging keeps one external stage table and uses unique staged
  files instead of concurrent SQL inserts into a shared stage table.
- When `transfer_keys` is used, per-batch transfer logs include the active
  transfer key values so long keyed transfers can be traced by slice.
- `transfer_keys` expressions should be deterministic and disjoint. The library
  rejects duplicate generated key tuples, but it cannot prove that arbitrary SQL
  expressions produce non-overlapping slices.
- `write_mode="upsert"` stages source rows and rejects duplicate staged keys
  before any target mutation. Greenplum then uses staged key delete-and-insert.
  Trino and ClickHouse require `upsert_partition_column`, build a final
  increment for affected partitions, drop those partitions, and insert the
  final increment. They do not use native `MERGE`, arbitrary key deletes, or
  ClickHouse lightweight key deletes for upsert finalization.
- Trino partition upsert requires `upsert_partition_drop_sql_template` in the
  target connection config. The template is rendered once per affected
  partition value with `{table}`, `{partition_column}`, and
  `{partition_value}`.
- Upsert dry-run plans use `table_schema` when provided, infer simple source
  query output columns when possible, and otherwise show an explicit
  source-column placeholder without opening database connections.
- ClickHouse targets create distributed/shard table pairs unless `ch_only_shard=True`.
- `target_rows_per_second`, `target_batch_seconds`, and `target_batch_memory_mb`
  are mutually exclusive adaptation controls: set at most one per call.
- By default, transfer validates row counts before finalizing the target. It
  counts the source query first, compares that count with streamed rows and the
  actual stage-table `COUNT(*)`, and fails before target writes when they differ.
  For keyed transfers, every rendered slice is counted and validated before the
  aggregate stage table is finalized.
- When the source connection defines `transfer_staging_schema`, snapshot
  materialization is the extraction mechanism regardless of public row-count
  validation. `validate_row_count=False` disables only the public
  source-to-target count comparison; transfer ID, destination, uniqueness, and
  ordinal coverage checks remain mandatory.
- A new call removes discoverable non-empty stages on its current source and
  target connections only when their stored exact destination matches and their
  transfer ID differs. Empty, malformed, unverifiable, legacy, and `load_df`
  stages are preserved. Historical source stages on another connection alias
  may require explicit `cleanup_stale_stage_tables(stage_tables=[...])` cleanup.
- For ClickHouse sources with no explicit `LIMIT`, row-count validation streams
  with `LIMIT <counted_source_rows>` and temporarily disables the client
  `query_limit` while opening the stream, so connection-level query caps do not
  silently truncate transfers.
- If a ClickHouse source stream fails mid-read with a transport/chunk error,
  the current staged attempt is aborted before finalization. When `full_retry_cnt`
  permits another full-transfer attempt, the retry restarts from scratch with
  half the previous transfer batch size, down to `min_batch_size`.
- Throughput-driven adaptation is default via
  `target_rows_per_second=True` (or omitted).
- Rows-per-second adaptation probes smaller batches first, then larger batches
  when smaller batches do not improve throughput. `adaptive_batch_size_step`
  controls each probe as a fraction of the current accepted size.
- If you set `target_batch_seconds`, `target_rows_per_second` is disabled.
- If you set `target_batch_memory_mb`, memory-based adaptation is used and
  takes precedence.
- `transfer_staging_schema` configured on the target connection keeps staging
  tables inside that schema. When configured, stale and overlapping staging tables
  for the target user are cleaned before and after each transfer.
- For Trino targets, `trino_mode=None` automatically uses Parquet
  object-storage staging when both `transfer_staging_schema` and
  `transfer_staging_location` are configured on the target connection. Set
  `trino_mode="parquet"` to require that path, or `trino_mode="values"` to force
  generated multi-row `INSERT ... VALUES` staging.
- Parquet transfer streams source rows into temporary Parquet files under
  `transfer_staging_location`, creates a Trino external stage table in
  `transfer_parquet_staging_schema` when configured (otherwise
  `transfer_staging_schema`), and then applies the normal `append`, `replace`,
  `truncate_insert`, or `upsert` finalization logic from that stage table.
- Python and Trino must both have access to the same object-storage prefix for
  Parquet staging. The Parquet/object-storage Python dependencies are installed
  with the package; if `transfer_staging_location` is not configured, Trino
  targets keep using the row-batch `INSERT` staging path.
- `sql.cleanup_stale_stage_tables()` is the public helper for explicit cleanup of
  user-scoped staging tables.
- Optional `min_batch_seconds`, `max_batch_seconds`, `min_batch_memory_mb`, and
  `max_batch_memory_mb` clamp their corresponding active target constraints
  when those targets are enabled.
- For Greenplum targets, `gp_insert_chunk_size` controls the initial
  `execute_values` page size when adaptive batching is enabled. If omitted, the
  initial page size is `10_000`; rows-per-second page adaptation uses the same
  `adaptive_batch_size_step`. Set `adaptive_batch_size=False` to keep the page
  size fixed.

[SQL functions index](index.md)
