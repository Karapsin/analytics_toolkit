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

`upsert` requires `key_columns`. Trino uses native `MERGE`, subject to
connector support at runtime. Greenplum uses staged delete-and-insert.
ClickHouse uses lightweight `DELETE` plus insert and does not require
`ReplacingMergeTree`.

## Public Helper Coverage

| Helper area | Greenplum | Trino | ClickHouse |
| --- | --- | --- | --- |
| read, execute, and execute-read workflows | yes | yes | yes |
| dataframe loading and SQL transfers | yes | yes | yes |
| table creation, dropping, metadata, and DDL extraction | yes | yes | yes |
| partition removal with [sql.drop_partitions](functions/drop_partitions.md) | yes | yes | yes |
| Greenplum partition creation and vacuum helpers | yes | no | no |
| ClickHouse shard/distributed table management | no | no | yes |

Greenplum is the transactional, maintenance-friendly backend. Trino is best
treated as a query and Iceberg table backend without portable table-size
metadata. ClickHouse has the most specialized DDL behavior because distributed
targets are managed as shard/distributed table pairs.

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
