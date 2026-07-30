[SQL functions index](index.md)

# show_tables

List tables visible through backend metadata as a dataframe.

```python
show_tables(db_key: 'str', schema: 'str | None' = None, conditions: 'str | None' = None, table_name: 'str | Sequence[str] | None' = None, ch_distributed_table_stats: 'bool' = False, trino_catalog: 'str | None' = None) -> 'pd.DataFrame'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias from `.connections`
- `table_name` - target or source table name, depending on the helper
- `schema` - schema/database filter for table listing
- `conditions` - SQL predicate evaluated against the normalized metadata aliases `db`, `schema`, `table_name`, `row_count`, and `table_size_bytes`

### Backend-Specific Inputs

- `ch_distributed_table_stats` - whether ClickHouse table listings should resolve distributed shard statistics
- `trino_catalog` - Trino catalog to query instead of `.connections[db_key].catalog`

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

Invalid identifiers and other deterministic metadata-query errors fail
immediately without retrying the same query.

[SQL functions index](index.md)
