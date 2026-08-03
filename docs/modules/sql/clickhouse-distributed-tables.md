[SQL module index](index.md)

# ClickHouse Distributed Tables

ClickHouse write helpers default to a distributed table backed by a managed
local shard table. This keeps user-facing reads pointed at the distributed table
while writes and DDL preserve the shard/distributed pair. The main workflows
are [sql.load_df](functions/load_df.md), [sql.transfer](functions/transfer.md),
and [sql.create_sql_table](functions/create_sql_table.md).

## Shard and Distributed Pair

The local shard table stores data. The distributed table routes reads and writes
through the configured cluster. The standard managed shard name is derived from
the requested target table.

Use shard-only mode only when the requested target should be a local table and
cluster-wide distributed behavior is not wanted.

## Cluster DDL

Cluster DDL is queued without making Python wait for the full asynchronous
ClickHouse DDL operation. The helper still checks local and cluster visibility
before inserting so lagging metadata does not silently break writes.

The helper preserves configured `Distributed` sharding expressions exactly.
In particular, `rand()` remains the integer-valued ClickHouse function rather
than being normalized to the floating-point `randCanonical()` function. Local
fallback creation for a replicated shard includes an explicit UUID when needed
by a `{uuid}`-based default replica path; the corresponding `ON CLUSTER`
statement remains unchanged.

Use [sql.ch_reconfigure_table](functions/ch_reconfigure_table.md) to replace a
stored Distributed cluster, sharding key, MergeTree engine, partition key,
sorting key, or table settings. The helper resolves cluster macros before
deciding whether a change stays on the current cluster or requires a
cross-cluster data migration.

Structural changes create and validate a replacement before cutover. Writers
must be paused during that window. Atomic and Shared databases use an atomic
name exchange; other database engines use a reversible rename sequence.

## Replace and Drop

Replace flows verify that old distributed and shard tables disappear before
recreate. If a host keeps stale metadata, per-host cleanup can retry local drops
on affected hosts before the replacement continues.

[sql.drop_tables](functions/drop_tables.md) removes managed table pairs.

[SQL module index](index.md)
