[SQL module index](index.md)

# Transfers

Transfers move rows from a source SQL query to a target table. They are the
right workflow when the source data is already in a database and Python should
coordinate extraction, batching, type mapping, and target writes. The public
entrypoint is [sql.transfer](functions/transfer.md).

The transfer flow has four conceptual steps:

1. Open source and target connections from `.connections`.
2. Inspect source query metadata when target creation or type casts need it.
3. Stream source rows in batches.
4. Insert staged rows and finalize the target table.

For Trino targets, a target connection with both `transfer_staging_schema` and
`transfer_staging_location` stages transfers from different connection keys
through Parquet files in object storage. The staging schema is the Trino table
namespace, while the staging location is the physical prefix that Python writes
and Trino reads. Without `transfer_staging_location`, Trino transfers keep the
row-batch `INSERT` staging behavior.

Use `write_mode` to choose finalization behavior:

- `append` inserts staged rows into the existing target.
- `replace` recreates or clears the target before inserting staged rows.
- `truncate_insert` clears the existing target shape before inserting staged rows.
- `upsert` requires `key_columns`, rejects duplicate staged keys, and replaces
  matching target keys before inserting staged rows.

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

For Greenplum targets, `gp_insert_chunk_size` is the initial `execute_values`
page size inside each transfer insert batch when adaptive batching is enabled.
If omitted, Greenplum transfer inserts start at `10_000` rows per page and then
adapt from measured rows per second with the same `adaptive_batch_size_step`.
Set `adaptive_batch_size=False` to keep an explicit `gp_insert_chunk_size`
fixed.

Progress totals are approximate unless a reliable total is known. Row estimates
come from backend planners and should be treated as progress hints, not counts.

## Retries

Operation retries reopen connections and retry the failed public operation.
Transfer-level retries restart the whole transfer flow, including source reads
and target staging. This is safer than resuming from an unknown partial batch,
but it means target write mode and staging behavior should be chosen with
restartability in mind.

## Types

Transfers prefer native source metadata over pandas-inferred batch types. When
the target already exists, final stage-to-target inserts cast staged values to
the target column types. Use `table_schema` when the target type must be
explicit and portable inference is not enough.

Upsert finalization is backend-specific. Trino uses native `MERGE`, so
connector-specific `MERGE` support is enforced by Trino at runtime. Greenplum
uses staged delete-and-insert. ClickHouse uses lightweight `DELETE` against the
local shard for distributed targets, then inserts through the distributed
target; it does not use `ReplacingMergeTree`.

[SQL module index](index.md)
