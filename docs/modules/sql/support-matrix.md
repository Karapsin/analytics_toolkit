[SQL module index](index.md)

# SQL Support Matrix

| Backend | Parser dialect | Transactions | Analyze | Distributed DDL | Write modes |
| --- | --- | --- | --- | --- | --- |
| `gp` | `postgres` | yes | yes | no | `append`, `replace`, `truncate_insert` |
| `trino` | `trino` | no | yes | no | `append`, `replace`, `truncate_insert` |
| `ch` | `clickhouse` | no | no | yes | `append`, `replace`, `truncate_insert` |

[SQL module index](index.md)
