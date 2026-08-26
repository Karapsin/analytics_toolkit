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
If that readiness check times out, staged load and transfer operations preserve
the partial table pair for policy-aware cleanup and retry the complete public
operation; they do not misclassify the partial create as a stage-name collision.
After the shard DDL scope is ready, the helper also verifies that the shard
table and expected schema exist on every host used by the effective
`Distributed(...)` routing cluster. A routing/scope mismatch fails immediately
with host-count diagnostics and guidance to correct `distributed.cluster`.

Every ClickHouse table-creation API accepts `ch_ddl_wait_policy`. Use
`wait_all` (default) for both physical and Distributed relations,
`wait_shard` for physical relations only, `wait_distr` for Distributed facades
only, or `wait_none` to skip post-create readiness polling. The explicit
argument overrides `.connections`; skipped checks do not alter DDL execution,
cutover, rollback, or cleanup behavior.

The helper preserves configured `Distributed` sharding expressions exactly.
In particular, `rand()` remains the integer-valued ClickHouse function rather
than being normalized to the floating-point `randCanonical()` function. Local
fallback creation for a replicated shard includes an explicit UUID when needed
by a `{uuid}`-based default replica path; the corresponding `ON CLUSTER`
statement remains unchanged.

## Connection-Wide Cluster Routing

For an alias whose reads, writes, and generated SQL should always fan out over
a cluster, configure the optional `cluster_routing` object in `.connections`.
It rewrites named read sources through the ClickHouse `cluster(...)` table
function, sends inserts to the corresponding cluster function with the
configured sharding key, and adds `ON CLUSTER` to supported DDL.

This is independent of managed shard/Distributed-pair topology. In particular,
named Distributed and `system` tables are still routed as named sources. An
explicit `ON CLUSTER` value from SQL, `ddl_defaults`, or a helper policy has
precedence over the connection default, so a macro such as `'{cluster}'` is
preserved and used by that statement. See
[Automatic ClickHouse Cluster Routing](configuration.md#automatic-clickhouse-cluster-routing)
for the configuration shape and validation rules.

Use [sql.ch_reconfigure_table](functions/ch_reconfigure_table.md) to replace a
stored Distributed cluster, sharding key, MergeTree engine, partition key,
sorting key, or table settings. The helper resolves cluster macros before
deciding whether a change stays on the current cluster or requires a
cross-cluster data migration.

Reconfiguration uses the same dedicated topology inputs as table creation:
`ch_shard_on_cluster` controls physical DDL, `ch_distributed_on_cluster`
controls facade DDL, and `ch_distributed_cluster` controls routing. This allows
a facade deployed on one cluster scope to route to shards on another. When a
shard is remote from the connected host, its DDL is inspected through the live
Distributed routing cluster. Use `to_defaults=True` to converge the managed
topology to the connection's regular `ddl_defaults` policy.

Structural changes create and validate a replacement before cutover. Writers
must be paused during that window. Atomic and Shared databases use an atomic
name exchange; other database engines use a reversible rename sequence.

## Replace and Drop

Replace flows verify that old distributed and shard tables disappear before
recreate. If a host keeps stale metadata, per-host cleanup can retry local drops
on affected hosts before the replacement continues.

[sql.drop_tables](functions/drop_tables.md) removes managed table pairs.

[SQL module index](index.md)
