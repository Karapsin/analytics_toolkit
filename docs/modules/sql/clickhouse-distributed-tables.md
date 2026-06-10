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

## Replace and Drop

Replace flows verify that old distributed and shard tables disappear before
recreate. If a host keeps stale metadata, per-host cleanup can retry local drops
on affected hosts before the replacement continues.

[sql.drop_tables](functions/drop_tables.md) removes managed table pairs.

[SQL module index](index.md)
