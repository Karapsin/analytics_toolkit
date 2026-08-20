[SQL module index](index.md)

# Transfers

Transfers move rows from a source SQL query to a target table. They are the
right workflow when the source data is already in a database and Python should
coordinate extraction, batching, type mapping, and target writes. The public
entrypoint is [sql.transfer](functions/transfer.md).

For a keyed source whose connection defines `transfer_staging_schema`, the
staged-source flow is a pipeline rather than a sequence of global phases:

1. Generate one transfer ID, resolve the exact canonical destination, open
   bounded source and target pools, and inspect source/target metadata once for
   the attempt.
2. Let a reader claim one key, acquire a live-stage credit, and create and count
   one immutable source table with CTAS.
3. Publish that table to a bounded ready-key queue. One writer owns the whole
   key, lazily creates its private target stage on its first non-empty key, and
   overlaps source reads and target inserts through a capacity-one RAM queue.
4. Validate the completed key in the target database, record its in-memory
   checkpoint, acknowledge it, and let a reader drop the exact source table.
5. After every key is verified and every acknowledgement is drained, validate
   and consolidate only the target stages that were created, then perform one
   final destination mutation.

There is no all-keys materialization barrier: one key can be in CTAS, another in
RAM batching, another in target insertion, and another in validation. Unkeyed
source staging retains one immutable slice-zero snapshot with bounded ordinal
ranges.

For Trino targets, a target connection with both `s3_transfer_staging_schema`
and `s3_transfer_staging_location` stages transfers from different connection
keys through Parquet files in object storage. The S3 staging schema is the
external-table namespace, while the location is the physical prefix that Python
writes and Trino reads. Without the complete pair, Trino transfers keep the
row-batch `INSERT` staging behavior. `transfer_staging_schema` remains the
separate namespace for non-Parquet SQL staging and source snapshots.

Use `write_mode` to choose finalization behavior:

- `append` is the default and inserts staged rows into the existing target.
- `replace` recreates or clears the target before inserting staged rows.
- `truncate_insert` clears the existing target shape before inserting staged rows.
- `upsert` requires `key_columns`, rejects duplicate staged keys, and applies
  backend-specific replacement semantics.

Omitting `write_mode`, or passing `None`, selects `append`. Existing callers
that require a full-table refresh must pass `write_mode="replace"` explicitly.
When the source returns zero rows, transfer leaves an existing target unchanged
for every write mode, including `replace`.

Use [sql.read](functions/read.md) instead when the goal is only to return a
source query as a dataframe. Use [sql.load_df](functions/load_df.md) when
Python already owns the rows. Use
[sql.create_sql_table](functions/create_sql_table.md) with `sql=` when the
source query schema should create a target table before any optional insert.

## Batching

`batch_size` is the initial fetch and insert size. Adaptive batching is enabled
by default and adjusts later batches from successful insert latency. The default
rows-per-second mode probes a smaller batch first; when that does not improve
throughput, it restores the accepted size and probes larger batches. Larger
batches with equivalent throughput are accepted until a later probe worsens and
rolls back. Use `adaptive_batch_size_step` to control each relative probe size.
Use memory targeting when row width varies enough that a fixed row count is a
poor proxy for process memory.

The row-count contract is per logical batch: a source range cannot return more
rows than the size captured when that range was scheduled. The ordinal filter
and SQL `LIMIT` enforce that bound, and the exact range count is checked before
queueing. Adaptive changes apply to later ranges only, so an already-prefetched
batch can be larger than a newly reduced size without exceeding its own
scheduled bound.

Lazy keyed staging permits one batch being inserted and one batch being read or
queued for each writer. It can therefore retain at most `2 × effective writers`
RowBatch payloads. In this mode, `target_batch_memory_mb` is an invocation-wide
approximate payload target divided evenly across those resident slots. It is a
feedback target rather than a hard process-memory cap: the first or an
unexpectedly wide batch can overshoot, and normalization and database-driver
copies are outside the RowBatch estimate.

For Greenplum targets, `gp_insert_chunk_size` is the initial `execute_values`
page size inside each transfer insert batch when adaptive batching is enabled.
If omitted, Greenplum transfer inserts start at `10_000` rows per page and then
adapt from measured rows per second with the same `adaptive_batch_size_step`.
Set `adaptive_batch_size=False` to keep an explicit `gp_insert_chunk_size`
fixed.

Transfer row-count validation is separate from progress estimates and is exact.
For lazy keyed source staging, every CTAS is counted before publication and each
requested ordinal batch must return its exact size. The writer then compares the
streamed count with target-stage rows and verifies the exact transfer ID,
canonical destination, slice ID, and complete unique ordinal range before that
key can be acknowledged. Aggregate validation runs again before finalization.
For ClickHouse sources, row-count protection also prevents connection-level
`query_limit` caps from silently truncating reads.

Each keyed source stage contains exactly one rendered key and is immutable after
CTAS. It consumes a live-stage credit until a reader drops it after target
validation. The limit is `effective readers + effective writers`, which bounds
concurrent source storage without eliminating total DDL/catalog churn; transfers
with very large key counts still create and drop one source table per key.
The nightly/manual stress profile exercises exactly 64 one-row keys on each
backend with four readers, three writers, and one-row batches. It verifies exact
target data and no residual attempt-owned source or target stages. This range is
a regression signal, not a universal production key-count limit: the live-stage
bound stays at seven in that scenario, while cumulative CTAS, DROP, and catalog
work still grows linearly with the number of keys.
Without source staging, transfer retains direct count-then-stream behavior and
requires keys for concurrency above one.

All transfer SQL stages start with a stable destination-hash prefix, while
stage rows store the exact canonical destination and full transfer ID. Hashes
are collision-resistant naming aids, not proof of ownership and never authorize
deletion. Runtime allocation checks existence and selects a collision-adjusted
name rather than reusing an existing table.

Only workers inside one `sql.transfer` invocation are supported concurrently.
Independent simultaneous calls to the same destination are unsupported.
Best-effort startup cleanup is not a lock or fencing mechanism, and the design
creates no manifest, lease, heartbeat, owner marker, bookkeeping or coordination
table, persistent work queue, coordination view, or sequence. Empty or ambiguous
stage names created by the current attempt are tracked for exact cleanup. Objects
whose ownership cannot be proved from exact runtime state or validated identity
are never dropped automatically and remain for explicit cleanup.

On a later invocation, an empty crash remnant can be cleaned without row
identity only when its generated name has the exact destination hash and a
different full transfer ID. Current-attempt and malformed names remain
protected. This rule relies on the documented restriction that independent
transfers to the same exact destination are unsupported.

## Lazy scheduling and resource bounds

Readers dynamically claim pending keys. A reader releases its source connection
after CTAS/count and after each batch read, before it can wait on a full queue.
Writers dynamically claim ready keys, but one key never moves between writers.
Each writer has at most one prefetched batch in its queue in addition to the
batch currently being inserted.

Source work prioritizes acknowledged drops, active-key reads, and then new CTAS
materialization. A reader processes at most one available drop between
consecutive batch handoffs, drains all pending acknowledgements before another
CTAS, and performs a mandatory complete drain at shutdown. Writers never mutate
the source database.

A writer begins without a target stage. Its first non-empty key creates the
writer's private stage, including ClickHouse physical companions when required,
and later keys reuse it. An unused writer or one assigned only empty keys creates
no target stage. Empty keys emit no batches but still undergo target-side
zero-row validation.

## Concurrency limits

Keyed transfers accept either the legacy combined `concurrency=N` setting or
independent `read_concurrency` and `write_concurrency` settings. The two forms
cannot be combined. An omitted split side resolves to `1`.

`soft_concurrency_cap` throttles the requested reader and writer counts
independently. The resulting two ceilings must each be at or below
`hard_concurrency_cap`, whose default is `5`; otherwise validation fails before
connection lookup. Available keys can reduce the final effective counts
further. These effective counts are also the per-transfer source and target
connection limits. Metadata inspection, CTAS/count, batch reads, stage creation,
inserts, validation, acknowledgement drops, retries, consolidation,
finalization, and cleanup all use the corresponding bounded pool. Queue waits
do not retain leases. Limits apply per alias and transfer invocation, not as a
global cap across aliases or processes; `read=1, write=1` opens at most one
Toolkit source and one Toolkit target connection at a time.

Dry-run plans expose requested, soft-limited, hard-cap, and effective counts.
For lazy keyed source staging they also identify the planned per-key source
stage count, the `effective readers + effective writers` live-source-stage
limit, capacity-one writer prefetch queues, and lazy target stages whose actual
count can be below the planned writer maximum when keys are empty. These plans
show each key's CTAS, count, validation, acknowledgement drop lifecycle and
conditional target-stage templates. They describe reader assignment as dynamic;
they do not claim a static reader-to-key or writer-to-key schedule.

## Progress, logging, and ETA

Every key-specific message starts with the same bounded tag, for example
`[slice=2/12 key=event_date:'2026-08-02']`. Composite keys share one tag. Values
use safe literal formatting and are truncated when necessary; unkeyed work uses
`[slice=1/1]`, and raw SQL, row contents, and credentials are never included.

Committed row and approximate RAM-byte totals advance only after a successful
target-stage commit. Each successful non-empty logical batch produces one line
with its end-to-end time from source-read start through insert completion,
batch rows/second, rolling global rows/second, approximate memory/second, load
ETA, and total transfer ETA. Concurrent rates use cumulative row deltas over
wall-clock time rather than averaging overlapping batch rates. Retries neither
double-count nor duplicate a logical batch completion. Memory rate is
approximate in-process RAM throughput in IEC units, not network or compressed
database bandwidth. These transfer messages are emitted independently of the
optional progress bar.

Attempt-average and rolling throughput begin at the earliest successful
batch's source-read start, including idle gaps between batches but excluding
metadata inspection, source counting, DDL, and other pre-loading work. The
reported total time still covers the full attempt. Concurrent batches that
commit out of read-start order rebase both rate anchors to the earliest
observed read start. Once the final batch commits, loading throughput is frozen
so consolidation and destination finalization reduce only their remaining work
components rather than progressively lowering the loading rate.

While some keyed CTAS counts are unknown, estimated total rows equal exact known
rows plus the average materialized-key size for each unknown key; affected load
totals and load ETA are marked `~`. ETA uses the lower positive value of rolling
global and attempt-average throughput and remains unavailable until two
successful non-empty batches establish a usable sample. A one-batch transfer
therefore reports elapsed duration only when it completes.

Load ETA covers only remaining loading. Total transfer ETA is always marked `~`
because it also estimates remaining non-primary-stage consolidation and final
destination DML as row-equivalent passes at loading speed. Loading and
consolidation components disappear as their phases complete; at finalization
start only estimated finalization remains. These estimates exclude
unpredictable locks, retries, backend maintenance, and cleanup failures. A full
retry resets rows/second, memory/second, progress, and both ETAs.

Representative messages are:

```text
[slice=2/12 key=event_date:'2026-08-02'] Staged batch 3: 50,000 rows; key 150,000/240,000; total 450,000/~1,320,000; batch time 3.2 seconds; total time 28 seconds; batch rate 15,625 rows/s; rolling rate 18,200 rows/s; approximate RAM rate 24.3 MiB/s; rolling approximate RAM rate 27.8 MiB/s; load ETA ~48 seconds; total transfer ETA ~2 minutes 3 seconds
[slice=2/12 key=event_date:'2026-08-02'] Retrying acknowledged source-stage drop: attempt 2/5; target checkpoint remains verified; rows will not be retransmitted
Completed source-stage loading: 1,320,000 rows in 1 minute 12 seconds; average rate 18,333 rows/s; average approximate RAM rate 26.9 MiB/s; remaining total transfer ETA ~29 seconds
```

Exact numbers and units depend on observed wall-clock timing and approximate
in-process batch size. Approximate totals and ETAs always carry `~`; unavailable
samples are reported explicitly rather than inferred from a single batch.

## Retries

Connection and operation retries use fresh connections within the bounded pools
and reuse the attempt's cached metadata. A CTAS/count failure does not publish
the key. A batch-read failure retains its immutable source table. A target-write
or validation failure does not acknowledge the key and therefore cannot trigger
its source drop.

After target validation, the in-memory checkpoint makes the key immutable for
that attempt. If its acknowledged source drop needs retrying, its target rows
are retained and never retransmitted. A persistent cleanup failure aborts before
the final destination mutation. Fatal or ambiguous key failures abort the full
attempt; when a full retry remains, metadata is inspected once again and every
key is rematerialized, including keys whose earlier source tables were already
dropped. Per-key CTAS operations and full retries can observe different source
moments, so no cross-attempt snapshot is promised.

Checkpoints cannot survive process restart. Startup/crash cleanup may discover
attempt-owned stages on the current aliases, but no manifest or coordination
table is available for resume. Finalization never treats retained staging as
proof that the destination mutation succeeded, and append is not retried after
an ambiguous final append.

ClickHouse finalization retries are narrower when the operation creates its
target from scratch: replace, or append/upsert when the target was absent. Each
`retry_cnt` attempt creates a clean target, waits for the normal DDL-readiness
deadline, and may then wait for `ch_ddl_ready_timeout_extension_cnt` additional
`timeout_increment` intervals. After insertion, the target count is polled over
the same bounded readiness windows so asynchronous Distributed delivery and
replica visibility can converge without rebuilding a still-populating target.
Exact target and stage counts must match even when `validate_row_count=False`.
An exhausted readiness deadline, insertion failure, or persistent count mismatch
drops only the incomplete target and repeats finalization from the preserved
stage. Source data is rematerialized through `full_retry_cnt` only after these
local attempts are exhausted. Existing-target append and upsert retain their
prior behavior because their total row counts cannot equal the incoming stage
count.

## Types

Transfers prefer native source metadata over pandas-inferred batch types. When
the target already exists, final stage-to-target inserts cast staged values to
the target column types. Use `table_schema` when the target type must be
explicit and portable inference is not enough. Same-Trino transfers preserve
the complete native source type signature, including arrays, maps, rows, and
nested combinations; cross-backend transfers retain portable type mapping.
Native UUID source metadata maps to `UUID` on Greenplum and Trino and to
`Nullable(UUID)` for an initially nullable ClickHouse stage. ClickHouse stage
nullability is refined from rows, and an explicit `table_schema` still takes
precedence.

Upsert finalization is backend-specific. Greenplum uses staged
delete-and-insert on `key_columns`. Trino and ClickHouse require
`upsert_partition_column`, build a final increment for affected partitions, drop
those partitions, and insert the final increment. Trino needs
`upsert_partition_drop_sql_template` in the target connection config so the
connector-specific partition drop syntax is explicit. Trino and ClickHouse do
not use native `MERGE`, arbitrary key deletes, or ClickHouse lightweight key
deletes for upsert finalization.

[SQL module index](index.md)
