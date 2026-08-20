[SQL functions index](index.md)

# transfer

Stream data from a source SQL query into a target table on another configured connection.

```python
transfer(from_db: 'str', to_db: 'str', from_sql: 'str | None' = None, to_table: 'str | None' = None, from_table: 'str | None' = None, write_mode: 'str | None' = 'append', batch_size: 'int' = 100000, adaptive_batch_size: 'bool' = True, min_batch_size: 'int' = 1000, max_batch_size: 'int | None' = None, adaptive_batch_size_step: 'float' = 0.1, target_rows_per_second: 'bool' = True, target_batch_seconds: 'float | None' = None, min_batch_seconds: 'float | None' = None, max_batch_seconds: 'float | None' = None, target_batch_memory_mb: 'float | None' = None, min_batch_memory_mb: 'float | None' = None, max_batch_memory_mb: 'float | None' = None, target_rows_per_second_window: 'int' = 5, target_rows_per_second_deadband: 'float' = 0.15, retry_cnt: 'int' = 5, timeout_increment: 'float' = 5, full_retry_cnt: 'int' = 5, full_timeout_increment: 'float' = 600, key_columns: 'str | Sequence[str] | None' = None, upsert_partition_column: 'str | None' = None, gp_distributed_by_key: 'str | Sequence[str] | None' = None, gp_partitions: 'Mapping[str, Any] | None' = None, gp_insert_chunk_size: 'int | None' = None, trino_insert_chunk_size: 'int | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str | None' = None, ch_cluster: 'str | None' = None, ch_sharding_key: 'str | None' = None, ch_distributed_table: 'bool | None' = None, ch_distributed_engine_template: 'str | None' = None, ch_distributed_cluster: 'str | None' = None, ch_shard_on_cluster: 'str | None' = None, ch_distributed_on_cluster: 'str | None' = None, ch_ddl_ready_timeout_seconds: 'float | None' = None, ch_ddl_ready_timeout_extension_cnt: 'int | None' = None, ch_ddl_wait_policy: 'str | None' = None, ch_only_shard: 'bool' = False, ch_retry_per_host_drops: 'bool' = True, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None, progress: 'bool' = False, estimate_total_rows: 'bool' = False, table_schema: 'dict[str, str] | None' = None, transfer_keys: 'str | Sequence[str] | Mapping[str, str] | None' = None, transfer_key_values: 'Sequence[Any] | Mapping[str, Sequence[Any]] | None' = None, concurrency: 'int | None' = None, read_concurrency: 'int | None' = None, write_concurrency: 'int | None' = None, ignore_source_staging: 'bool' = False, trino_mode: 'TrinoTransferMode | None' = None, validate_row_count: 'bool' = True, ch_count_limit_read: 'bool' = True, soft_concurrency_cap: 'int | None' = None, hard_concurrency_cap: 'int' = 5) -> 'int | SqlPlan | SqlOperationResult'
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
- `target_batch_memory_mb` - approximate in-process RowBatch memory target used for adaptive transfer batches; lazy keyed staging treats it as an aggregate target across active and prefetched batches
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
- `soft_concurrency_cap` - optional shared ceiling applied independently to requested reader and writer counts before slice limits; a larger request is throttled rather than rejected
- `hard_concurrency_cap` - safety ceiling for each soft-limited reader and writer count; defaults to `5` and rejects either side above the cap before connection lookup
- `ignore_source_staging` - when `True`, ignore the source connection's `transfer_staging_schema` for this call and use direct streaming; target-side staging is unchanged
- `partition_by` - partitioning columns or expression for created tables, interpreted according to the target backend
- `order_by` - ordering or sorting columns or expression for created tables, interpreted according to the target backend

### Backend-Specific Inputs

- `gp_distributed_by_key` - distribution key column or columns for created Greenplum target tables
- `gp_partitions` - initial Greenplum range or list child definitions used when the final target is created
- `gp_insert_chunk_size` - initial Greenplum transfer stage insert page size; omitted values start at `10_000`
- `trino_insert_chunk_size` - number of rows per Trino parameterized multi-row insert statement
- `trino_mode` - Trino target staging mode: `"parquet"` forces object-storage Parquet staging, `"values"` forces generated multi-row `INSERT ... VALUES`, and `None` prefers Parquet whenever the target connection has the complete `s3_transfer_staging_schema`/`s3_transfer_staging_location` pair; ordinary `transfer_staging_schema` does not override that preference
- `ch_engine` - nullable override for the configured ClickHouse shard engine
- `ch_cluster` - deprecated compatibility shortcut that fills both execution clusters and the routing cluster; use the dedicated fields below
- `ch_sharding_key` - nullable override for the configured Distributed sharding expression
- `ch_distributed_table` - nullable topology override; true creates a shard/Distributed pair and false creates one physical table
- `ch_distributed_engine_template` - nullable override for the configured `Distributed(...)` engine template
- `ch_distributed_cluster` - nullable override for the routing cluster used as the first `Distributed(...)` argument
- `ch_shard_on_cluster` - nullable override for the shard/physical-table `ON CLUSTER` target
- `ch_distributed_on_cluster` - nullable override for the Distributed facade's independent `ON CLUSTER` target
- `ch_ddl_ready_timeout_seconds` - overall ClickHouse post-create readiness deadline; explicit values override the connection default
- `ch_ddl_ready_timeout_extension_cnt` - additional `timeout_increment` readiness intervals allowed for each fresh-target finalization attempt; explicit values override the connection default of `1`
- `ch_ddl_wait_policy` - `wait_all`, `wait_shard`, `wait_distr`, or `wait_none`; explicit values override the ClickHouse connection policy
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
    soft_concurrency_cap=3,
    hard_concurrency_cap=5,
)
```

Here the requested `6/2` reader/writer pair becomes a soft-limited `3/2`
ceiling. Available keys can reduce either side further. `dry_run=True` exposes
the requested, soft-limited, hard-cap, and final effective values in both plan
options and metadata, together with the effective source and target connection
limits.

When the source connection defines `transfer_staging_schema`, keyed transfers
use a lazy pipeline with no global materialization barrier. A reader claims one
key, creates and counts one immutable source table with CTAS, releases its
source connection, and publishes that exact table to a ready-key queue. A
writer claims the whole key and creates its private target stage only when its
first non-empty key arrives. The reader and writer then overlap through that
writer's capacity-one RAM batch queue; another reader can materialize a later
key while an earlier key is being inserted or validated.

The source and target pools are bounded by effective read and write concurrency.
At most `effective readers + effective writers` keyed source tables are live at
once. After all batches for a key commit, in-memory range and batch
acknowledgements verify its exact streamed count. Only then is the key
acknowledged; a reader drops the exact acknowledged source table and releases
its live-stage credit. Aggregate database stage counts are checked before any
final target mutation. A key is never split between writers, and a writer that
handles only empty keys creates no target stage. Independent per-key CTAS
operations may observe slightly different source moments.

Nightly/manual stress coverage runs exactly 64 one-row keyed slices per backend
with four readers, three writers, and `batch_size=1`, then checks exact target
rows and absence of attempt-owned source and target stages. That fixed range is
a regression signal rather than a universal production limit. The live-stage
ceiling remains seven for the scenario, but total per-key CTAS, DROP, and catalog
churn remains linear in the number of keys.

`batch_size` is a per-logical-batch row bound. Each source range is scheduled
with a captured size; its ordinal predicate and SQL `LIMIT` both enforce that
bound, and its exact row count is checked before the RowBatch is queued.
Adaptive changes apply only to later ranges, so a batch already prefetched may
be larger than a newly reduced size while still respecting the size with which
it was scheduled. A writer may hold the batch it is inserting plus one
prefetched batch; across `W` effective writers, at most `2 × W` RowBatch
payloads are resident in this pipeline.

For lazy keyed staging, `target_batch_memory_mb` is divided across those
`2 × W` resident slots. It is an aggregate approximate payload target, not a
hard process-memory ceiling: adaptation reacts to measured RowBatch size,
initial or unexpectedly wide batches can overshoot transiently, and driver,
normalization, dataframe, and database-client copies are not included.

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
  appears in source/target stage names, Parquet resources, query labels,
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
  may move between slices and writers while immutable logical batch identifiers
  and row counts preserve in-memory validation. Stage rows remain until the
  single final target operation succeeds or attempt cleanup runs.
- Split concurrency is limited to keyed transfers. Legacy `concurrency=N`
  retains combined `N/N` behavior; unkeyed source-staged transfers retain their
  existing single-snapshot implementation.
- The same soft/hard cap pair is applied independently to readers and writers.
  Without a soft cap, requested counts are the ceilings checked against
  `hard_concurrency_cap`. All cap and concurrency values must be built-in
  positive integers; booleans are rejected.
- A configured source `transfer_staging_schema` creates one immutable source
  stage per key with CTAS and drops it only after that key is committed and
  validated in the target database. There is no all-keys phase barrier. Target
  stages are private per writer and are created lazily on that writer's first
  non-empty key for SQL/VALUES staging. Parquet mode instead shares one external
  target stage across writers while preserving those source stages and their
  validation guarantees. Only stages actually created are validated and
  consolidated. Unkeyed transfers continue to use slice zero in one source
  snapshot with bounded ordinal ranges.
- Keyed source staging inspects one representative source query and any required
  target metadata once per full attempt. The ordered source schema, native and
  mapped types, `table_schema` overrides, source-local paging columns, insert
  order, and stage-DDL inputs are reused for every key. Connection retries do
  not refresh this contract; a full-attempt retry inspects it once again.
- Unkeyed source-staged transfers keep at least one worker and use no more than
  `min(concurrency, ceil(total_rows / batch_size))` workers. Logs report both
  requested and effective counts.
- The keyed ready queue is bounded, each writer has one capacity-one prefetch
  queue, and the live source-stage limit is effective readers plus effective
  writers. Queue waits hold no database connection. Source and target pools
  also cover metadata, retries, drops, validation, consolidation, finalization,
  and cleanup; helpers do not open connections outside those limits.
- Every logical source range contains at most the `batch_size` captured when it
  was scheduled. Adaptive changes affect subsequent ranges only. Lazy keyed
  staging can retain one active and one prefetched RowBatch per writer. When
  `target_batch_memory_mb` is set, its approximate payload target is shared
  evenly across those `2 × effective writers` resident slots rather than being
  applied independently to every slot.
- Internal snapshot, source-batch, stage-count, ordinal, and superseded-stage
  queries use distinct info-level action and phase labels in transfer logs.
- Transfer SQL stage names use one identifier policy on every backend: at most
  63 UTF-8 bytes, beginning with a stable 16-hex destination hash and carrying
  the full transfer ID and worker/role identity.
  Catalog/schema qualification and quoting remain backend-specific. Within the
  configured staging schema, the destination hash, full transfer ID, reserved
  worker/role suffix, and collision suffix form the automatic-cleanup authority.
  Malformed, current-attempt, legacy, `load_df`, and other-destination names are
  protected. The configured staging schema should therefore be reserved for
  toolkit-managed resources.
- Target-bound SQL/VALUES batches, Parquet files, and target stages contain only
  user columns. Transfer ID, destination, slice ID, and row ordinal are never
  repeated in transported rows. Source snapshots retain only collision-resolved
  slice and ordinal columns for local bounded paging; range queries filter and
  order by them but project only user columns across the network.
- No manifest, lease, owner marker, heartbeat, bookkeeping, or other persistent
  coordination table, view, work queue, or sequence is created. Scheduling,
  acknowledgements, verified-key checkpoints, progress, and ETA state exist only
  in memory for the current attempt and cannot resume after process restart.
- A CTAS/count failure never publishes its key. A source-read, target-write, or
  validation failure retains that key's source stage and prevents final target
  mutation. Once target validation succeeds, cleanup retries never retransmit
  the verified key; a persistent acknowledged-source drop failure still aborts
  before finalization. A fatal key error restarts the complete attempt when a
  full retry is available, refreshes metadata once, and rematerializes every
  key. It may therefore observe newer source data.
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
  With source staging, each rendered slice is materialized by its own lazy CTAS,
  counted, streamed to one writer, validated, acknowledged, and dropped while
  other slices can occupy different phases. On the compatibility path without
  source staging, workers stream assigned slices into private target stages as
  before. Created worker stages are consolidated before one final target write.
  Trino Parquet staging keeps one external stage table and uses unique staged
  files instead of concurrent SQL inserts into a shared stage table.
- Every key-specific message starts with one stable, bounded tag such as
  `[slice=2/12 key=event_date:'2026-08-02']`; composite keys remain in the same
  tag, and unkeyed work uses `[slice=1/1]`. Values use safe literal formatting
  and logs never include raw SQL, row contents, or credentials. The same tag
  follows materialization, batches, validation, retries, acknowledgement
  cleanup, and errors.
- Every successful non-empty logical batch emits one completion line. Batch
  time spans source-read start through successful target insert, including queue
  wait. Logs report batch and rolling global rows/second plus approximate RAM
  memory/second in IEC units; this is in-process RAM throughput, not network or
  compressed database bandwidth. Committed totals advance only after
  target-stage commit, remain monotonic under concurrency, and do not
  double-count retries.
- Load ETA is distinct from total transfer ETA. While keyed counts are still
  unknown, total rows are estimated from known counts and the mean materialized
  key size, and load values are prefixed with `~`. Unkeyed source staging uses
  its exact snapshot count minus committed target-stage rows. ETA uses the lower
  positive value of rolling global and attempt-average rows/second and stays
  unavailable until two successful non-empty batches. Total transfer ETA is
  always marked approximate because it also models remaining consolidation and
  finalization as row-equivalent work. Completed phases are removed from that
  estimate, and a full-attempt retry resets all rate, memory, progress, and ETA
  samples.
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
- By default, transfer validates row counts before finalizing the target.
  Unkeyed flow counts its source snapshot. Lazy keyed flow captures the exact
  count immediately after each CTAS and compares it with acknowledged streamed
  rows. Mandatory aggregate database stage counts still run before the final
  destination mutation. A fresh ClickHouse Distributed target is then polled
  over its bounded DDL-readiness windows until its visible count matches the
  stage, preventing transient delivery or replica lag from causing an immediate
  rebuild.
- When the source connection defines `transfer_staging_schema`, snapshot
  materialization is the extraction mechanism regardless of public row-count
  validation. `validate_row_count=False` disables only the public
  source-to-target count comparison; streamed-to-stage payload counts,
  uniqueness, target-key overlap, and in-memory range coverage remain mandatory.
- A new call removes discoverable empty or non-empty stages on its current
  source and target connections when their reserved collision-safe name has the
  exact destination hash and a different full transfer ID. This covers crashes
  after partial loading without persistent per-row ownership metadata.
  Current-attempt, malformed, legacy, `load_df`, and other-destination names
  remain protected. Because simultaneous calls for one destination are
  unsupported, a newer call may remove an older still-running call's stage.
  Historical source stages on another connection alias may require explicit
  `cleanup_stale_stage_tables(stage_tables=[...])` cleanup.
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
  takes precedence. It is feedback from approximate RowBatch payload size, not
  a hard Python-process memory ceiling. Lazy keyed source staging divides the
  configured aggregate target across one active and one prefetched slot per
  effective writer.
- `transfer_staging_schema` configured on the target connection keeps staging
  tables inside that schema. When configured, stale and overlapping staging tables
  for the target user are cleaned before and after each transfer.
- For Trino targets, `trino_mode=None` automatically uses Parquet
  object-storage staging only when both `s3_transfer_staging_schema` and
  `s3_transfer_staging_location` are configured on the target connection. Set
  `trino_mode="parquet"` to require that path, or `trino_mode="values"` to force
  generated multi-row `INSERT ... VALUES` staging.
- Hive Parquet stages represent UUID and timezone-aware timestamp columns as
  strings because those native Iceberg target types are unsupported by Hive
  external tables. UUID strings and offset-bearing timestamp strings are cast
  back to the target's declared native types during finalization. Timestamp
  columns without time zones use microsecond precision in the Hive stage to
  match Trino's configured Parquet timestamp precision.
- Parquet transfer streams source rows into temporary Parquet files under
  `s3_transfer_staging_location`, creates a Trino external stage table in
  `s3_transfer_staging_schema`, and then applies the normal `append`, `replace`,
  `truncate_insert`, or `upsert` finalization logic from that stage table.
- Python and Trino must both have access to the same object-storage prefix for
  Parquet staging. The Parquet/object-storage Python dependencies are installed
  with the package; if the complete S3 pair is not configured, Trino targets
  keep using the row-batch `INSERT` staging path and
  `transfer_staging_schema` remains the ordinary SQL staging namespace.
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
