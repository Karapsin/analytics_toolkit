[SQL module index](index.md)

# SQL Support Matrix

The SQL module exposes one public facade across Greenplum, Trino, and
ClickHouse, but generated SQL and metadata richness differ by backend. Use this
matrix as a quick check before choosing a workflow or expecting a generated plan
to look the same across engines.

For Python checks, use `from analytics_toolkit import sql` and inspect
`sql.BACKEND_CAPABILITIES`. The capability rows are derived from the internal
backend registry in `analytics_toolkit.sql.backends`, so support-matrix output
and backend dispatch share the same canonical backend list.

## Backend Capabilities

| Backend | Name | SQL dialect | Identifier quote | Transactions | Analyze | Stage tables | Distributed tables |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `gp` | Greenplum | `postgres` | `"` | yes | yes | yes | no |
| `trino` | Trino | `trino` | `"` | no | yes | yes | no |
| `ch` | ClickHouse | `clickhouse` | `` ` `` | no | no | yes | yes |

## Mutation Semantics

| Backend | Create behavior | Drop behavior | Truncate behavior | Write modes |
| --- | --- | --- | --- | --- |
| `gp` | append-only columnar `CREATE TABLE` defaults | `DROP TABLE IF EXISTS` | `TRUNCATE TABLE` | `append`, `replace`, `truncate_insert`, `upsert` |
| `trino` | `CREATE TABLE` with Parquet/object-store layout | `DROP TABLE IF EXISTS` | `DELETE FROM` | `append`, `replace`, `truncate_insert`, `upsert` |
| `ch` | `MergeTree` shard plus optional `Distributed` pair | `DROP TABLE IF EXISTS`, plus distributed pair handling when requested | `TRUNCATE TABLE IF EXISTS` | `append`, `replace`, `truncate_insert`, `upsert` |

`upsert` requires `key_columns`. Greenplum uses staged delete-and-insert and
does not require partition replacement. Trino and ClickHouse require
`upsert_partition_column`, build a final increment for affected partitions,
drop those partitions, and insert the final increment. Trino also requires
`upsert_partition_drop_sql_template` in the target connection config.

## Public Helper Coverage

The executable integration classification lives in
[`integration/sql_coverage_manifest.json`](../../../integration/sql_coverage_manifest.json).
It is checked against `analytics_toolkit.sql.__all__`, public signatures,
registered adapters, write modes, and the complete 3×3 transfer-pair set.

| Helper area | Greenplum | Trino | ClickHouse |
| --- | --- | --- | --- |
| read, execute, and execute-read workflows | yes | yes | yes |
| dataframe loading and SQL transfers | yes | yes | yes |
| table creation, dropping, metadata, and DDL extraction | yes | yes | yes |
| partition removal with [sql.drop_partitions](functions/drop_partitions.md) | yes | yes | yes |
| Greenplum partition creation and vacuum helpers | yes | no | no |
| ClickHouse shard/distributed table management | no | no | yes |

Greenplum is the transactional, maintenance-friendly backend. Trino uses an
Iceberg target catalog and a separate Hive external-table catalog for temporary
Parquet staging; stage objects and metastore entries are removed after every
attempt. ClickHouse has the most specialized DDL behavior because distributed
targets are managed as shard/distributed table pairs.

The integration matrix uses distinct source and target aliases even for
same-backend transfers, and declares every Greenplum/Trino/ClickHouse source and
target pair plus every target write mode in the schema-version-2 manifest.

## Integration Type Contract

| Logical value | Greenplum | Trino/Iceberg | ClickHouse |
| --- | --- | --- | --- |
| boolean | `BOOLEAN` | `BOOLEAN` | `Nullable(Bool)` |
| signed integer | `BIGINT` | `BIGINT` | `Nullable(Int64)` |
| fixed decimal | `NUMERIC(18,4)` | `DECIMAL(18,4)` | `Nullable(Decimal(18,4))` |
| floating point | `DOUBLE PRECISION` | `DOUBLE` | `Nullable(Float64)` |
| Unicode text | `TEXT` | `VARCHAR` | `Nullable(String)` |
| date | `DATE` | `DATE` | `Nullable(Date)` |
| UTC timestamp | `TIMESTAMPTZ` | `TIMESTAMP(6) WITH TIME ZONE` | `Nullable(DateTime64(6, 'UTC'))` |
| UUID | `UUID` | `UUID` | `Nullable(UUID)` |
| canonical JSON | `JSONB` | `VARCHAR` | `String` |

Integration comparisons normalize decimals to scale four, timestamps to UTC
microsecond ISO-8601, UUIDs to lowercase, JSON with sorted keys, and backend
null/scalar wrappers to Python values. Column and deterministic row order must
still match exactly.

## Backend Extension Notes

Backend implementations live under `analytics_toolkit/sql/backends/<backend>/`.
The public facade remains `from analytics_toolkit import sql`; generic public
functions accept configured connection keys, not backend objects.

A normal in-repo backend addition should add a backend package with adapter and
config/opening logic, register it in `analytics_toolkit.sql.backends.registry`,
then add focused behavior tests and documentation. Legacy compatibility imports
under `analytics_toolkit.sql.backend_adapters` and
`analytics_toolkit.sql._backend_adapters` resolve to the canonical registry.

[SQL module index](index.md)
