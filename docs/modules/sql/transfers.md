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

Parquet files use the resolved Hive stage schema for every column, including
all-null columns and nested `ARRAY`, `MAP`, and `ROW` values. UUIDs, timezone
aware timestamps, and timestamps above precision 6 use exact strings recursively
where the Hive stage declares `VARCHAR`. Finalization casts those transport
values into the resolved native destination types without changing its connector
or table format. Decimal values retain their declared precision and scale. Named row
fields retain their exact identifiers. Positional rows receive stable field names
inside the temporary stage and are cast back by position into the destination
schema. Unsupported resolved stage types fail before stage DDL or file upload;
the writer never guesses a replacement physical type from a sample.

Use `write_mode` to choose finalization behavior:

- `replace` is the default and builds a validated replacement from staged rows before cutover.
- `append` inserts staged rows into the existing target.
- `truncate_insert` clears the existing target shape before inserting staged rows.
- `upsert` requires `key_columns`, rejects duplicate staged keys, and applies
  backend-specific replacement semantics.

Omitting `write_mode`, or passing `None`, selects `replace`. Pass
`write_mode="append"` explicitly for incremental loads. When the source returns
zero rows, the default empty-source behavior matches the mode: replace and
truncate materialize a zero-row result, while append and upsert keep the target.
Set `empty_source_policy="keep"` or `"error"` to override that behavior.

Use [sql.read](functions/read.md) instead when the goal is only to return a
source query as a dataframe. Use [sql.load_df](functions/load_df.md) when
Python already owns the rows. Use
[sql.create_table](functions/create_table.md) with `sql=` when the
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
For lazy keyed source staging, every materialized snapshot is counted before
publication and each requested ordinal batch must return its exact size. With a
cluster-routed ClickHouse source, materialization creates an empty `MergeTree`
on every routing-cluster host, waits for shard readiness, and populates one
logical copy through `cluster(...)`; later reads use `clusterAllReplicas(...)`.
The writer then compares the streamed count with target-stage rows and verifies the exact transfer ID,
canonical destination, slice ID, and complete unique ordinal range before that
key can be acknowledged. Aggregate validation runs again before finalization.
For ClickHouse sources, row-count protection also prevents connection-level
`query_limit` caps from silently truncating reads.

Each keyed source stage contains exactly one rendered key and is immutable after
materialization. It consumes a live-stage credit until a reader drops it after target
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
target from scratch: append/upsert/replace when the target was absent. Each
`retry_cnt` attempt creates a clean target, waits for the normal DDL-readiness
deadline, and may then wait for `ch_ddl_ready_timeout_extension_cnt` additional
`timeout_increment` intervals. After insertion, the target count is polled over
the same bounded readiness windows so asynchronous Distributed delivery and
replica visibility can converge without rebuilding a still-populating target.
Exact target and stage counts must match even when `validate_row_count=False`.
An exhausted readiness deadline, insertion failure, or persistent count mismatch
drops only the incomplete target and repeats finalization from the preserved
stage. Source data is rematerialized through `full_retry_cnt` only after these
local attempts are exhausted. Existing-target replace performs its own
validated replacement and reversible cutover; ambiguous rollback failures are
not replayed. Existing-target append and upsert retain their prior behavior
because their total row counts cannot equal the incoming stage count.

## Types

Transfers prefer native source metadata over pandas-inferred batch types. When
the target already exists, final stage-to-target inserts cast staged values to
the target column types. Use `table_schema` when the target type must be
explicit and portable inference is not enough. Same-Trino transfers preserve
the complete native source type signature, including arrays, maps, rows, and
nested combinations; cross-backend transfers retain portable type mapping.
Same-ClickHouse transfers retain native widths through 256 bits, Date32,
DateTime64 precision and timezone, and nested native types. Inferred Trino
timestamps use explicit precision 6 for the supported Iceberg contract.

When source metadata cannot prove an exact destination representation, transfer
profiles the complete selected input before creating or mutating the destination.
Every keyed slice contributes to one schema. The adaptive path freezes the rows
in a run-owned, length-framed, versioned checkpoint and replays those rows rather
than running a mutable source query again. Declared types that fit retain the
streaming path. Explicit `table_schema` and existing destination types remain
authoritative: inference never widens an existing table or changes its numeric
columns into text. Explicitly requested timestamp precision reduction is a
deliberate caller conversion. Date values supplied to timestamp columns become
exact midnight datetimes, using an explicitly named ClickHouse timezone when
present, otherwise the resolved actual write timezone. Their midnight ticks
must fit the destination calendar and precision range before destination mutation. ClickHouse `FixedString(N)` is validated in UTF-8
bytes, and `Enum8`/`Enum16` values must belong to the declared labels or codes.
Unrecognized authoritative native types require proven identical source metadata
or typed-stage validation before the operation can certify them.

ClickHouse timestamp interpretation is bound to the operation's actual write
timezone and policy version, separately from SQL schema identity. Explicit
column zones take precedence; implicit declarations keep their original SQL
type. Validation and certified replay require the same context, so a retry
cannot silently adopt a different timezone. Unknown context is never replaced
with an assumed UTC policy; plans leave required live resolution pending.
Named zones use portable IANA rules, including future DST transitions.
Authoritative timestamp schemas reject ambiguous or nonexistent naive local
times before mutation. Inference preserves those unresolved wall times, and
mixed naive/aware values without an explicit disambiguation policy, as exact
text. Aware and raw timestamps already identify an instant and are not
relocalized.

Internal ClickHouse source streams retain raw UTC ticks before dataframe
construction, including nested `DateTime` and `DateTime64` values. Two instants
in a repeated DST hour therefore remain distinct through streaming, profiling
and replay. Logical source SQL types remain unchanged; UTC is an internal
transport representation. Physical ClickHouse timestamps infer timezone-aware
Greenplum and Trino timestamp types recursively when native precision fits.
The normal public read mode and its outer result shapes remain unchanged.

If a structural cutover becomes uncertain, the certified local input is retained
with a private versioned manifest containing its schema, source identity digest,
row count and checksum. Recovery validates all frames, the checksum and count
before replay. Ordinary completion and failures before mutation remove local
checkpoints.

Inference tries exact integers, decimal/numeric and temporal/native nested types
before text. Every inferred native candidate is validated against the complete
profile before destination publication. Date/timestamp calendar bounds and raw
tick ranges apply during inference too: values outside ClickHouse native ranges
use exact ISO text. Aligned timestamps try a supported lower native precision
before text; empty/all-null temporal columns retain suitable native defaults.
Decimal scale and integral digits include every selected value;
integers and Decimal values never pass through binary floating point. If no
supported native type preserves the column, text uses exact base-10 integers,
fixed-point Decimal, ISO dates/timestamps (aware instants normalized to UTC), and
canonical UUIDs. Structured fallback is compact typed JSON version 1, carrying
source backend/type and recursively tagged values; maps use ordered pairs so
duplicate keys survive. Unsupported binary text uses a `base64:` prefix; native
binary transport remains preferred. ClickHouse `String` input is profiled before
cross-backend text mapping: raw bytes use native Greenplum `BYTEA` or Trino
`VARBINARY`. A string column containing NUL uses Greenplum `BYTEA` with exact
UTF-8 encoding; ordinary valid strings retain `TEXT`. Existing authoritative
Greenplum text columns reject raw bytes, NUL and invalid Unicode before mutation.
SQL NULL remains NULL. Mixed naive/aware
timestamps use typed text without assigning a timezone to naive values.

Nested inference also tries native Trino `ARRAY`, `MAP` and `ROW`, and native
Greenplum arrays. Greenplum arrays must be rectangular within each value;
ragged arrays, NULL or empty subarrays, and unsupported dimensions use typed
text. A Greenplum array column can contain different rectangular ranks across
rows, so a cross-backend transfer profiles every selected row before choosing
a fixed-rank destination. Greenplum maps do not cause automatic JSONB creation.
Trino maps require non-NULL, unique keys; duplicate keys or coercions that merge
different keys use typed text during inference and fail validation for an
authoritative map schema. ClickHouse maps preserve duplicate ordered pairs.
For Trino, uniqueness uses destination-native key identity, including UUID normalization,
`REAL` rounding and `CHAR` padding. Distinct source keys that become the same
destination key fail validation before target mutation; an entry is never
silently overwritten. For example, uppercase and lowercase spellings of one
UUID cannot both become keys in an authoritative `MAP(UUID, VARCHAR)`.
The check applies recursively and preserves legal ClickHouse duplicate pairs.
Map metadata can authorize streaming only when each key conversion is proven
to preserve distinct destination-native keys. Scalar value-domain fit alone
does not prove that: bounded `VARCHAR(2)` keys can still collide in `CHAR(2)`.
The proof recurses through arrays, rows and maps nested in values; otherwise
the complete selected input is validated before target mutation. An explicit
staging schema cannot bypass the unchanged existing target's key constraints:
both source-to-stage and stage-to-target conversions must be safe. Valid casts
keep the requested stage schema and converted rows. Same-type and proven safe
widening conversions retain lazy streaming.
These rules follow [Greenplum's array implementation](https://raw.githubusercontent.com/greenplum-db/gpdb-archive/main/src/backend/utils/adt/arrayfuncs.c),
[Trino's strict map construction](https://raw.githubusercontent.com/trinodb/trino/master/core/trino-main/src/main/java/io/trino/operator/scalar/MapFromEntriesFunction.java),
[Trino's native type semantics](https://trino.io/docs/current/language/types.html#char),
and [ClickHouse's map contract](https://clickhouse.com/docs/reference/data-types/map).

Greenplum can use unconstrained `NUMERIC` when the required precision exceeds
the explicit precision limit of 1000. Its native numeric storage allows up to
131072 integral and 16383 fractional digits; larger values use text when types
are inferred. The limits come from [Greenplum's numeric header](https://raw.githubusercontent.com/greenplum-db/gpdb-archive/main/src/include/utils/numeric.h)
and its [numeric storage representation](https://raw.githubusercontent.com/greenplum-db/gpdb-archive/main/src/backend/utils/adt/numeric.c).

Query dry runs never profile or spool full data. Their `SqlPlan.options` identify
pending adaptive resolution and native candidates. SQL-only generation that
cannot express this pending step requires an explicit `table_schema`.
Native UUID source metadata maps to `UUID` on Greenplum and Trino and to
`Nullable(UUID)` for an initially nullable ClickHouse stage. ClickHouse stage
nullability is refined from rows, and an explicit `table_schema` still takes
precedence. Greenplum `bytea` maps to ClickHouse `String`; HTTP and native
transports preserve the original byte sequence, including embedded nulls and
bytes that are not valid UTF-8.

Existing-target upserts require complete writable rows, including default-bearing
columns and excluding generated/noninsertable columns. Missing or extra columns
fail before staging or target mutation; reordered inputs use target column order.
When `upsert_partition_column` is supplied, the effective key is the ordered,
deduplicated combination of that column and `key_columns` on every backend.
The same ID in different partitions is a different record. Without a partition
column, the key retains `key_columns` semantics.

Upsert finalization is backend-specific. Greenplum uses transactional staged
delete-and-insert on the effective key. Trino and ClickHouse require
`upsert_partition_column`, build a final increment for affected partitions, drop
those partitions, and insert the final increment. Trino needs
`upsert_partition_drop_sql_template` in the target connection config so the
connector-specific partition drop syntax is explicit. Trino and ClickHouse do
not use native `MERGE`, arbitrary key deletes, or ClickHouse lightweight key
deletes for upsert finalization. Their complete preservation/incoming payload is
prepared before partition mutation and retained across transient replay. A retry
never rereads preservation rows from a partially replaced target. If finalization
exhausts its retry budget, recovery payload stages are retained.

Key-validated append selects and inserts the named incoming columns in their
original order, so subsets and reordered inputs have ordinary append semantics.
Stage candidates are visible before CREATE, but only acknowledged resources are
owned and eligible for cleanup. Collisions and uncertain CREATE outcomes are
retained without deleting unrelated resources.

[SQL module index](index.md)
