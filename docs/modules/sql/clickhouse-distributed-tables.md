[SQL module index](index.md)

# ClickHouse Distributed Tables

ClickHouse write helpers default to a distributed table backed by a managed
local shard table. This keeps user-facing reads pointed at the distributed table
while writes and DDL preserve the shard/distributed pair. The main workflows
are [sql.load_df](functions/load_df.md), [sql.transfer](functions/transfer.md),
and [sql.create_table](functions/create_table.md).

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
function, sends inserts to the corresponding cluster function, and adds
`ON CLUSTER` to supported DDL. Managed-pair inserts use the verified facade
sharding key; direct physical-table inserts use the configured key. Explicit
insertion functions retain their specified key.

```json
{
  "ch": {
    "type": "ch",
    "host": "one-host-from-core",
    "cluster_routing": {
      "cluster": "core",
      "sharding_key": "rand()"
    }
  }
}
```

Before rewriting a live table, routing compares every expected
shard/replica position from `system.clusters` with `system.tables`. Engine and
normalized DDL must agree on every host; replicated tables must also have one
Keeper path per shard, distinct replica names, and healthy replica metadata.
Probe errors, missing expected metadata, or inconsistent engines raise
`ClickHouseClusterTopologyError` before user SQL is submitted.

For a toolkit-standard managed pair, routing verifies that the local
Distributed facade points to the same-database `<table>_shard` relation. If
that shard exists at every expected routing-cluster position, named reads and
all insert paths route the physical shard directly. The availability policy
below runs before submission: `stop` rejects incomplete physical coverage, while
`healthy_shards` explicitly selects the usable subset. Inserts use foreground
Distributed delivery so success means acknowledged delivery. Metadata errors
remain errors under either policy.

Replicated and shared engines use `cluster(...)`, which selects one replica per
shard. Consistent non-replicated tables on a multi-replica cluster use
`clusterAllReplicas(...)` because each host is assumed to own distinct data.
Single-replica non-replicated tables use `cluster(...)`. Views and non-standard
Distributed facades stay local; a view's own SQL remains responsible for its
sources. Toolkit stages and `system` tables use all replicas. Exact explicit
`cluster(...)`/`clusterAllReplicas(...)` wrappers around a named table are
normalized to the same engine-dependent choice; unrelated table functions are
left unchanged.

Consequently, `wait_shard` is compatible with routing when the physical shard
has full routing-cluster coverage and consistent metadata. The confirmed
incomplete-pair fallback requires the local facade to be ready because it is
intentionally not rewritten. An explicit
`ON CLUSTER` value from SQL, `ddl_defaults`, or a helper policy has precedence
over the connection default, so a macro such as `'{cluster}'` is preserved and
used by that statement. See
[Automatic ClickHouse Cluster Routing](configuration.md#automatic-clickhouse-cluster-routing)
for the configuration shape and validation rules.

Private `sql.transfer` stages are the deliberate exception to one-replica
reads: their non-replicated `MergeTree` parts are read with
`clusterAllReplicas(...)` so concurrent inserts can land on different replicas
without losing rows during consolidation or validation. Cluster-routed source
snapshots first create empty tables on every replica, wait for shard readiness,
and populate only one replica per shard, so the same all-replica read remains
reconnect-safe without multiplying rows. Final replicated targets continue to
use normal one-replica-per-shard routing.

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

## Unavailable Shards

`ch_unavailable_shards="stop"` is the default for production processes.
`ch_unavailable_shards="healthy_shards"` is an explicit ad hoc mode: work with
what is currently available. The keyword applies to [sql.read](functions/read.md),
[sql.execute](functions/execute.md), [sql.load_df](functions/load_df.md),
[sql.transfer](functions/transfer.md), table helpers, and parallel execution.
An explicit argument overrides the alias default in `.connections`; `None`
uses that default, which is `stop` when the field is absent. See
[connection configuration](configuration.md#clickhouse-outage-default).

For `core` with two independent shards and one unavailable shard:

| Operation | `stop` | `healthy_shards` |
| --- | --- | --- |
| Read existing data | raise before the user query | read usable shards; result may be incomplete |
| Create a new table | raise before DDL | create only on selected healthy hosts |
| Append rows | raise before insertion | deliver every supplied row to selected healthy shards; exact `rand()` required |
| Insert from a query | raise | stream the partial source through acknowledged destination batches |
| Replace, upsert, truncate, drop, reconfigure an existing target | raise | raise while any participating scope is incomplete |
| Check target key overlap globally | raise | raise when the target is incomplete |
| No usable shard, conflicting schema, permission or SQL errors | raise | raise |

A missing replica is different from a missing shard. For replicated tables,
one usable replica per shard can preserve logical coverage. For independent
non-replicated replicas, every host owns separate data and losing one means
incomplete coverage. A reachable replica with expired coordination metadata
blocks reads and writes under both policies; a read-only replica blocks writes.
Cluster routing cannot safely exclude those reachable replicas. Reads of replicated
tables also stop if a reachable replica lacks the physical table while another
replica in the same shard has it, because the
original cluster route could select it or skip its entire shard. An unreachable
replica can still fail over to a table-ready replica in the same shard. If an
entire shard lacks the table, ad hoc reads can skip that shard and report partial
coverage without provisioning it.
Partial-source INSERT SELECT uses separate streaming and delivery connections, including for
native transport. Destructive cluster DDL still requires all participating
hosts. Preflight is a snapshot: a later failure can still interrupt an operation.
The configured entry server must be reachable; this policy does not select an
alternate entry server when the initial connection fails.

Existence and schema checks for existing replicated data also use replica
failover. `stop` still requires all participating hosts for cluster DDL, including
creation and removal of transfer stages. A transfer using local staging can
append to an existing replicated target through a surviving replica without
cluster DDL; a transfer configured to create stages `ON CLUSTER` must wait for
all DDL hosts.

Ad hoc operations emit a prominent stderr banner, bold red on terminals that
support color, a red notebook banner when IPython is active, and
`ClickHouseDegradedClusterWarning`. A completion banner records the observed
coverage and whether recovery artifacts were retained. Partial coverage observed
earlier remains visible even if schema provisioning later completes. `print_queries=False`
does not suppress these warnings. With `return_metadata=True`, supported read,
load, and transfer results expose `metadata.ch_cluster_status` (expected,
selected, and excluded hosts and shards) and `metadata.data_complete`.
Transfers additionally report `source_data_complete` and `target_data_complete`.
`parallel_sql` and `async_sql` pass the policy default to each task, with
per-task overrides taking precedence. Tasks keep separate coverage, retry,
cleanup and metadata state; a degraded task does not restrict its siblings.
Row-count validation in ad hoc mode validates the observed source rows; it does
not establish that an unavailable shard had no additional data.
Partial-source transfers require a materialized source snapshot and row-count
validation. Configure source transfer staging and leave `ignore_source_staging=False`;
unstaged partial sources are refused, including when validation is disabled.
Counts and delivery then use the same snapshot even if live values change while
preserving the row count.
`ClickHouseClusterUnavailableError` identifies a rejected coverage requirement;
`ClickHouseClusterTopologyError` identifies unsafe or inconsistent topology.

Coverage and topology rejections stop the current operation immediately;
the SQL retry parameters do not poll until a shard recovers. Production
schedulers can retry the whole job after recovery. Ordinary transient
connection failures before a live write retain the normal retry behavior.
Once a live write has been submitted, a later failure conservatively stops
automatic retries for the entire operation, including a multi-statement batch,
and retains recovery artifacts. This prevents replaying an earlier successful
mutation while retrying a later failed statement.
New toolkit-private stages are disposable under both policies, including stages
created with `IF NOT EXISTS`: the toolkit checks absence in the actual creation
scope before claiming ownership. A transient failure while populating an owned
stage can be cleaned up and retried with fresh connections. A pre-existing table
with a stage-like name is never claimed merely because `IF NOT EXISTS` succeeds.

Coverage enforcement adds metadata round trips: table definitions, cluster
membership and reachability, and, where applicable, settings and replica health.
These observations are reused within the operation but refreshed on the next
operation, so small queries can incur noticeable latency. Plain local-table
operations without a cluster scope do not fan out to other hosts, but still
inspect local metadata to distinguish local and Distributed tables.

### How healthy-only table creation works

`CREATE TABLE new AS existing_table` copies the source definition without
copying rows, including defaults and inherited storage properties. CTAS with
`AS SELECT ...` populates the new table through acknowledged delivery.
In `healthy_shards`, CREATE AS with an explicit column schema is refused before
mutation. Use separate CREATE and population steps when specifying types,
renamed columns, defaults, constraints or indexes. This avoids replacing the
requested schema with the inferred SELECT schema. `stop` retains native CTAS behavior.
A physical `_shard` table without its logical facade is an ambiguous partial
pair, not a new target: reconcile it before loading. Healthy-only `IF NOT EXISTS`
creation checks the complete definition, including engine, keys, indexes and
settings; conflicting definitions are rejected. Appends to verified existing
targets use their established definitions instead of recreating them from defaults.
Replace-style loads also prove absence in the resolved per-call creation scopes,
so an orphan physical table on another healthy host cannot contribute stale rows
when the alias has no automatic routing or cluster defaults.

DDL defaults do not enable automatic SQL routing. With `cluster_routing=None`,
plain physical-table reads, inserts, CREATE and DROP stay local. Explicit
`ON CLUSTER` and Distributed source tables retain their cluster semantics.
For a local target with a partial cluster source, use separate CREATE and
INSERT SELECT statements; a partial-source local CTAS is refused before creation.

The toolkit probes configured membership and reachable hosts, then inspects
database engines and existing table definitions. It does not change `core` or
create a temporary cluster definition. For a new managed pair, it creates the
physical `<table>_shard` relation and the Distributed facade separately, honoring
each configured DDL scope. Each selected host receives local `CREATE TABLE`
without `ON CLUSTER`. Offline hosts receive no queued DDL. This matters because
[ClickHouse distributed DDL](https://clickhouse.com/docs/reference/statements/distributed-ddl)
can execute queued statements when a host returns after a timeout.

All selected hosts must confirm the expected schema before loading. Atomic
physical tables share a stable UUID so default replicated-engine identities
remain consistent. Later provisioning copies the authoritative existing
schema and UUID; conflicting definitions are rejected rather than overwritten.
For replicated engines, every selected host's macros, effective Keeper path,
and replica name are validated **before any CREATE**. Independent shards must
have different paths and replicas within a shard must have distinct names.
Healthy-only new replicated tables require UUID-isolated paths; multi-shard
paths must contain `{shard}`. New replicated tables therefore require an Atomic
database; Ordinary remains supported for non-replicated tables. Explicit server
macro overrides follow [ClickHouse replicated-engine rules](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/replication).
Nested or escaped identity templates and unverified UUID overrides are refused.
Server-default engine arguments require readable
`system.server_settings`; otherwise specify explicit macro-based arguments.
Unverifiable or colliding identities are refused before mutation.
Replicated or Shared database engines are refused for healthy-only creation:
local DDL there could propagate beyond the selected hosts. Atomic and Ordinary
databases are supported. The client must be able to connect directly to selected
remote hosts; native connections use their configured cluster ports, while HTTP
connections use the alias HTTP port. Credentials and TLS settings come from
the alias.

Reads do not provision schemas. If shard 2 returns without the new table, an
ad hoc read continues using table-ready hosts and warns. A later
`healthy_shards` write creates matching **empty** tables on the recovered healthy
hosts, verifies them, and can use both shards for future random writes.
Historical data stays on its original shard. Native replication can refill a
recovered replica of that same shard; the toolkit does not copy historical rows
or join independent shards to one Keeper group. `stop` does not implicitly repair
missing tables. For example, 100 rows written to shard 1 during the outage stay
on shard 1 after shard 2 returns; recovery does not redistribute those 100 rows.

### Write delivery and recovery

When a retry deliberately drops a disposable snapshot, its retained creation
identity is cleared. Recreating it populates the observed source again before
counting and delivery. Finalization inserts into an empty target created by the
same operation; it does not require a replacement of that newly created target.

Degraded writes require the effective sharding key to be exactly `rand()`.
Deterministic keys, parameterized random expressions, unknown dependencies, and
nonstandard managed pairs are rejected when safe routing cannot be established.
Cross-cluster facade/data routing remains supported in strict mode; ad hoc
routing requires their routing clusters to agree. Batch selection uses original
shard identities and synchronous delivery with skipping disabled on the insert.
This prevents missing destinations from silently dropping supplied rows. Read
skipping is kept separate: ad hoc `INSERT SELECT` uses a client dataframe stream
and therefore has additional network and memory cost per batch.

A successful acknowledgement cannot make an operation atomic across shards.
If a connection fails after submission, some rows or DDL may already have been
accepted. Such mutations are not automatically replayed, including driver
`TypeError` failures after submission. Stages and backups are retained when the
outcome is uncertain. Destructive DDL uncertainty raises
`AmbiguousSqlReplaceError`; inspect the retained state before manual recovery.
Raw settings that contradict the selected policy are rejected. Neither mode
turns authentication, schema, or query errors into an empty successful result.

## Replace and Drop

Replace flows build and populate offside physical and Distributed tables,
validate row counts and a destination fingerprint, then exchange names in an
Atomic/Shared database or use a reversible rename sequence. The old target is
not dropped before the replacement is ready. A failed rollback raises
`AmbiguousSqlReplaceError` and preserves new `__replace_guarded_...` recovery
artifacts across operations. Unresolved guarded artifacts and active replacement
work block another replacement until reconciled. After verified successful cutover,
cleanup tables receive a `__discardable` suffix before best-effort deletion;
inactive completed leftovers and legacy artifacts retain automatic cleanup.
If marking a cleanup table fails after a verified successful cutover, the
replacement still succeeds and logs a warning. The affected artifacts remain
for reconciliation and are not automatically replayed or deleted.
An inactive guarded artifact alone is insufficient evidence for deletion: inspect
all shards and reconcile the current target and recovery tables first.
Failure to inspect recovery metadata also stops a replacement before mutation.

Aliases without `cluster_routing` retain native SQL text, including joins between
Distributed tables and local dimensions. Distributed sources are preflighted
without turning other tables into cluster-function reads. In `stop`, native
commands and syntax outside SQLGlot's dialect pass through after known declared
cluster scopes are checked; unmodeled query scopes are not inferred.

[sql.drop_tables](functions/drop_tables.md) removes managed table pairs.

[SQL module index](index.md)
