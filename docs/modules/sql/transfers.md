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

Use [sql.read](functions/read.md) instead when the goal is only to return a
source query as a dataframe. Use [sql.load_df](functions/load_df.md) when
Python already owns the rows. Use
[sql.create_table_from_sql](functions/create_table_from_sql.md) when the source
query schema should create a target table before any optional insert.

## Batching

`batch_size` is the initial fetch and insert size. Adaptive batching is enabled
by default and adjusts later batches from successful insert latency. Use memory
targeting when row width varies enough that a fixed row count is a poor proxy
for process memory.

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

[SQL module index](index.md)
