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
- `conditions` - backend-native metadata predicate appended to the table-listing query

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

`conditions` uses the backend's physical metadata columns. For example,
ClickHouse exposes the table name as `name` in `system.tables`:

```python
tables = sql.show_tables(
    "ch",
    schema="analytics",
    conditions="name LIKE '%temp_users%'",
)
```

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
