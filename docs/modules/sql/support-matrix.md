[SQL module index](index.md)

# SQL Support Matrix

The SQL module exposes one facade across Greenplum, Trino, and ClickHouse, but
the backends do not have identical capabilities. Use this matrix as a quick
check before choosing a workflow or expecting a generated plan to look the same
across engines.

Use `analytics-toolkit sql support-matrix` for the formatted text output.
For programmatic checks, import internal helpers from
`analytics_toolkit.sql.core.capabilities`.

| Backend | Parser dialect | Transactions | Analyze | Distributed DDL | Write modes |
| --- | --- | --- | --- | --- | --- |
| `gp` | `postgres` | yes | yes | no | `append`, `replace`, `truncate_insert` |
| `trino` | `trino` | no | yes | no | `append`, `replace`, `truncate_insert` |
| `ch` | `clickhouse` | no | no | yes | `append`, `replace`, `truncate_insert` |

Greenplum is the transactional, maintenance-friendly backend. Trino is best
treated as a query and Iceberg table backend without portable table-size
metadata. ClickHouse has the most specialized DDL behavior because distributed
targets are managed as shard/distributed table pairs.

[SQL module index](index.md)
