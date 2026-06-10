[SQL module index](index.md)

# SQL Support Matrix

The SQL module exposes one public facade across Greenplum, Trino, and
ClickHouse, but generated SQL and metadata richness differ by backend. Use this
matrix as a quick check before choosing a workflow or expecting a generated plan
to look the same across engines.

Use `analytics-toolkit sql support-matrix` for the compact CLI view. For Python
checks, use `from analytics_toolkit import sql` and inspect
`sql.BACKEND_CAPABILITIES`.

## Backend Capabilities

| Backend | Name | SQL dialect | Identifier quote | Transactions | Analyze | Stage tables | Distributed tables |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `gp` | Greenplum | `postgres` | `"` | yes | yes | yes | no |
| `trino` | Trino | `trino` | `"` | no | yes | yes | no |
| `ch` | ClickHouse | `clickhouse` | `` ` `` | no | no | yes | yes |

## Mutation Semantics

| Backend | Create behavior | Drop behavior | Truncate behavior | Write modes |
| --- | --- | --- | --- | --- |
| `gp` | append-only columnar `CREATE TABLE` defaults | `DROP TABLE IF EXISTS` | `TRUNCATE TABLE` | `append`, `replace`, `truncate_insert` |
| `trino` | `CREATE TABLE` with Parquet/object-store layout | `DROP TABLE IF EXISTS` | `DELETE FROM` | `append`, `replace`, `truncate_insert` |
| `ch` | `MergeTree` shard plus optional `Distributed` pair | `DROP TABLE IF EXISTS`, plus distributed pair handling when requested | `TRUNCATE TABLE IF EXISTS` | `append`, `replace`, `truncate_insert` |

`upsert` is reserved by the write-mode validator but is not currently supported
by any backend.

## Public Helper Coverage

| Helper area | Greenplum | Trino | ClickHouse |
| --- | --- | --- | --- |
| read, execute, and execute-read workflows | yes | yes | yes |
| dataframe loading and SQL transfers | yes | yes | yes |
| table creation, dropping, metadata, and DDL extraction | yes | yes | yes |
| partition removal with [sql.drop_paritions](functions/drop_paritions.md) | yes | yes | yes |
| Greenplum partition creation and vacuum helpers | yes | no | no |
| ClickHouse shard/distributed table management | no | no | yes |

Greenplum is the transactional, maintenance-friendly backend. Trino is best
treated as a query and Iceberg table backend without portable table-size
metadata. ClickHouse has the most specialized DDL behavior because distributed
targets are managed as shard/distributed table pairs.

[SQL module index](index.md)
