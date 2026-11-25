[SQL functions index](index.md)

# show_tables

List tables visible through backend metadata as a dataframe.

```python
show_tables(db_key: 'str', schema: 'str | None' = None, conditions: 'str | None' = None, table_name: 'str | Sequence[str] | None' = None, ch_distributed_table_stats: 'bool' = False, trino_catalog: 'str | None' = None, *, ch_unavailable_shards: 'str | None' = None) -> 'pd.DataFrame'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias from `.connections`
- `table_name` - target or source table name, depending on the helper
- `schema` - schema/database filter for table listing
- `conditions` - SQL predicate evaluated against the normalized metadata aliases `db`, `schema`, `table_name`, `row_count`, and `table_size_bytes`

### Backend-Specific Inputs

- `ch_unavailable_shards` - ClickHouse outage policy: `None` uses the connection default (`stop` when omitted); `healthy_shards` enables ad hoc partial work with prominent warnings; see [outage policies](../clickhouse-distributed-tables.md#unavailable-shards)

- `ch_distributed_table_stats` - whether ClickHouse table listings should resolve distributed shard statistics
- `trino_catalog` - raw Trino catalog name to query instead of `.connections[db_key].catalog`

## Usage

```python
from analytics_toolkit import sql

tables = sql.show_tables("gp", schema="sandbox", table_name=["orders", "events"])
print(tables)
```

For Trino aliases without a configured catalog, pass the catalog explicitly:

```python
tables = sql.show_tables(
    "trino",
    trino_catalog="iceberg",
    conditions="table_name like '%contact_daily_transactions_pl_registered%'",
)
```

`trino_catalog` and `.connections[db_key].catalog` take raw catalog names. The
toolkit supplies SQL identifier quoting and escapes literal quote characters,
which remain part of the catalog name.

The same normalized condition works across Greenplum, Trino, and ClickHouse.
Wildcard syntax is passed through unchanged, so callers provide `%` and `_`
directly:

```python
tables = sql.show_tables(
    "ch",
    schema="analytics",
    conditions="table_name LIKE '%temp_users%'",
)
```

`table_name` remains an exact string or exact-name-list filter and can be
combined with `schema` and `conditions`.

Output example:

```python
tables[["schema", "table_name"]]
#      schema table_name
# 0   sandbox    orders
# 1   sandbox    events
```

ClickHouse listings deduplicate exact database/table identities. Case-distinct
names remain distinct. With automatic cluster routing, row counts and byte sizes
are logical cluster estimates: replicated/shared data is counted once per shard
using a deterministic available replica, while independent host data is summed
across hosts. `ch_distributed_table_stats=True` additionally resolves Distributed
facades from their underlying storage; the flag does not change ordinary local
table aggregation. Counts are metadata estimates rather than transactional counts.

`conditions` applies to the final logical totals, after replica selection and
aggregation. Unknown counts or sizes remain NULL, including unsupported view
estimates. Replicated totals require a usable representative and verified
shared replication identity; independent Keeper namespaces cannot be silently
counted as copies of one shard.

Incomplete collection never certifies a partial total as complete. Inspect
`tables.attrs["data_complete"]` and `tables.attrs["ch_cluster_status"]` for coverage;
unavailable facade statistics keep their existing unknown/default values with
`data_complete=False`.
Coverage attributes also remain attached to empty ClickHouse listings, with the
normalized five-column shape preserved. An empty partial listing still carries
`data_complete=False` and its cluster status, so it cannot confirm that matching
tables are absent. Complete empty listings retain their complete coverage status.

Invalid identifiers and other deterministic metadata-query errors fail
immediately without retrying the same query.

[SQL functions index](index.md)
