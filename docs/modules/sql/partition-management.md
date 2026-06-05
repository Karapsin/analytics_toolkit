[SQL module index](index.md)

# Partition Management

## General Functions

Use `drop_many_partitions` to remove several partition values from one target
table in input order. The helper picks backend-specific SQL from the connection
key's `type`.

```python
sql.drop_many_partitions(
    "gp",
    "sandbox.events",
    ["2025-05-01", "2025-05-02"],
)

sql.drop_many_partitions(
    "trino",
    "sandbox.events",
    ["2025-05-01", "2025-05-02"],
    partition_column="dt",
)
```

`transfer_table` reads native source query column types before loading data.
Stage and newly created target tables use the closest matching target backend
types instead of pandas-inferred batch types. Final stage-to-target inserts use
explicit column lists and cast staged columns to the target types. When
`replace_target_table=False` and the target already exists, the existing target
column types are used for those final casts.
Pass `table_schema={"column": "TYPE"}` to any helper that creates tables when
you need explicit backend-native column types instead of inferred types. The
schema must include exactly the columns being created; source DataFrame/query
column order is preserved when it is available.

```python
sql.load_df(
    "gp",
    "sandbox.scores",
    scores_df,
    table_schema={"user_id": "TEXT", "score": "NUMERIC(10, 2)"},
)

sql.transfer_table(
    from_db="gp",
    to_db="trino",
    from_sql="select user_id, score from sandbox.scores",
    to_table="sandbox.scores_copy",
    table_schema={"user_id": "VARCHAR", "score": "DECIMAL(10, 2)"},
)
```

`create_table_from_sql` uses the same native metadata mapping to create an
empty target table by default. Pass `insert_data=True` to insert the source
query result after creation. Existing targets are preserved by default; pass
`drop_target_if_exists=True` to drop the target first. When `table_db` is
omitted, the table is created on `source_db`. Cross-backend inserts delegate to
`transfer_table` with `replace_target_table=False` after the target is created.
Decimal precision and scale from source metadata are only preserved when valid
for the target backend; unbounded or out-of-range numerics fall back to the
backend's safe default decimal type. Binary source columns are preserved as
`BYTEA` on Greenplum targets and `VARBINARY` on Trino targets; ClickHouse uses
`String` for binary payloads.

## Backend-Specific Functions

Use `gp_create_many_partitions` to create several Greenplum partitions in input
order. Exactly one of `intervals`, `values`, `days`, `weeks`, `months`, or
`years` must be passed. Range partitions use `START (...) INCLUSIVE END (...)
EXCLUSIVE`; list partitions use `VALUES (...)`. Week inputs must already be
Mondays, month inputs must be first-of-month dates, and year inputs must be
January 1 dates.

```python
plan = sql.gp_create_many_partitions(
    "gp",
    "sandbox.events",
    days=["2026-05-01", "2026-05-02"],
    dry_run=True,
)

sql.gp_create_many_partitions(
    "gp",
    "sandbox.events",
    intervals=[
        {"start": "2026-05-01", "end": "2026-05-02"},
        {"name": "p_custom", "start": "2026-05-02", "end": "2026-05-03"},
    ],
    name_template="part_{}",
)

sql.gp_create_many_partitions(
    "gp",
    "sandbox.events_by_country",
    values=["RU", "KZ"],
)
```

Use `build_gp_create_many_partitions_sqls` to render the Greenplum DDL without
opening a connection.

Greenplum `drop_many_partitions` emits one native
`ALTER TABLE ... DROP PARTITION` statement per value, or `TRUNCATE PARTITION`
when `gp_truncate=True`. Trino uses a single Iceberg-style
`DELETE FROM ... WHERE <partition_column> IN (...)`, so `partition_column` is
required there. ClickHouse drops partitions from the managed shard table, for
example `<target>_shard`, on the default `{cluster}` macro.

For ClickHouse transfer targets, metadata-derived types are refined from the
first non-empty batch before creating the stage and target tables: columns
observed with nulls stay `Nullable(...)`, while columns with no observed nulls
use plain ClickHouse types. This keeps partition/order keys such as `Date`
non-nullable when the transferred data does not contain nulls.

For ClickHouse targets, `load_df`, `transfer_table`, and
`create_table_from_sql` create a local `<target>_shard` table first and then
create the requested target as a `Distributed` table. Use `partition_by`,
`order_by`, `ch_engine`, `ch_cluster`, and `sharding_key` to control the
shard DDL and distributed sharding expression. The default `ch_cluster` is the
ClickHouse `{cluster}` macro so created distributed/shard table pairs are
visible across the full cluster on Yandex Managed ClickHouse.
Pass `only_shard=True` to `load_df`, `transfer_table`,
`create_table_from_sql`, `create_sql_table`, or `ch_create_table_as` when the
requested ClickHouse table should be the local table itself. In that mode the
helpers do not create `<target>_shard`, do not create a `Distributed` table, and
do not submit `ON CLUSTER` DDL. Replace and truncate modes drop or truncate only
the requested local target table.
The shard table is also created locally without `ON CLUSTER` so the initiating
host does not have to wait for asynchronous cluster DDL before it can see its
own shard table. For local replicated shard DDL, the helper adds an explicit
table UUID because ClickHouse only allows the `{uuid}` macro in replicated
engine arguments when the table UUID is explicit or the statement uses
`ON CLUSTER`.
Append loads also re-submit the idempotent `CREATE TABLE IF NOT EXISTS`
statements before inserting, which repairs cases where an existing Distributed
target is present locally but its shard table is not yet visible on every host.
When the default `{cluster}` macro is used, the visibility poll resolves it with
`getMacro('cluster')`; passing the concrete cluster name such as
`ch_cluster="core"` avoids that extra lookup.
Cluster DDL is submitted with `distributed_ddl_task_timeout=0`, so ClickHouse
queues the `ON CLUSTER` operation without making Python hold the DDL request
open. Replacing an existing ClickHouse distributed table pair verifies the drop
with `clusterAllReplicas(..., system, tables)` before recreate; if a target or
shard remains on any host, the error lists the leftover `hostName()`, database,
table, and engine, and states that `ch_retry_per_host_drops=True` may be used
for a direct local-drop cleanup attempt. Public ClickHouse replace helpers enable
that retry by default; when leftover `hostName()` values match configured
`system.clusters.host_name` values, cleanup connects only to those affected
hosts, otherwise it falls back to all configured cluster hosts. Local host cleanup
runs concurrently with `ch_retry_per_host_drops_concurrency`, which defaults to
`5` when host retry is enabled and `None` when it is disabled. Before inserting,
the helper checks the configured cluster host count and polls
`clusterAllReplicas(..., system, tables)` until the shard table is visible on
every cluster host, then polls `clusterAllReplicas(..., system, columns)` until
the shard exposes the expected column types everywhere.
When replacing an existing distributed table pair, recreate paths use
`CREATE OR REPLACE TABLE` for cluster DDL. New targets still use
`CREATE TABLE IF NOT EXISTS`. This prevents stale shard or distributed metadata
from being reused on a lagging host without relying on `OR REPLACE` for first
creation.

`ch_create_table_as` is ClickHouse-only. It drops any existing target
distributed/shard table pair, creates a new `<target>_shard` table from the
provided query schema, creates the target `Distributed` table, and inserts the
query result into the distributed target. It accepts the same ClickHouse DDL
options as `load_df`: `partition_by`, `order_by`, `ch_engine`,
`ch_cluster`, and `sharding_key`. Its default `ch_cluster` is the ClickHouse
`{cluster}` macro so the created tables are visible across the full cluster on
Yandex Managed ClickHouse.
Pass `only_shard=True` to create and insert into `table_name` as a local
ClickHouse table instead of creating the distributed/shard pair.

If `ch_create_table_as` fails with `UNKNOWN_TABLE` for a small CTE joined by the
query, ClickHouse may be resolving that CTE name on a remote shard as a physical
table, for example `default.trigger_map`. Use `GLOBAL LEFT JOIN` syntax for the
CTE right side:

```sql
WITH trigger_map AS (
    SELECT 1 AS id
)
SELECT events.id
FROM default.events_source AS events
GLOBAL LEFT JOIN trigger_map AS trigger_map
    ON events.id = trigger_map.id
```

`ch_drop_table` is ClickHouse-only. It drops the requested distributed table and
its managed shard table, using `<table>_shard` by default. Pass `shard_table`
when the shard table has a custom name, and pass `ch_cluster=None` to skip
`ON CLUSTER` drop statements. If `table` itself ends with the standard
`_shard` suffix and `shard_table` is omitted, `ch_drop_table` treats it as a
single local shard table and drops only that table.

`ch_full_table_move` is ClickHouse-only. It reads `SHOW CREATE TABLE` for
`move_table`, extracts the source shard table from its `Distributed` engine, and
then reads that shard DDL. It creates the destination shard/distributed pair
with the same columns, types, engine clauses, settings, and sharding expression.
By default the destination uses the ClickHouse `{cluster}` macro; pass
`ch_cluster=None` to reuse the cluster extracted from the source DDL. It copies
rows with `INSERT INTO <to_table> SELECT * FROM <move_table>`, then drops the
source pair.

[SQL module index](index.md)
