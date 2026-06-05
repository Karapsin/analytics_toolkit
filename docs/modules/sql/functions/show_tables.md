[SQL functions index](index.md)

# show_tables

List tables visible through backend metadata as a dataframe.

```python
show_tables(db_key: 'str', schema: 'str | None' = None, conditions: 'str | None' = None, table_name: 'str | Sequence[str] | None' = None, ch_distributed_table_stats: 'bool' = False) -> 'pd.DataFrame'
```

## Inputs

- `db_key`: Connection key or alias from `.connections`.
- `schema`: Schema/database filter for table listing.
- `conditions`: Backend-native metadata predicate appended to the table-listing query.
- `table_name`: Target or source table name, depending on the helper.
- `ch_distributed_table_stats`: Whether ClickHouse table listings should resolve distributed shard statistics.

## Usage

```python
from analytics_toolkit import sql

tables = sql.show_tables("gp", schema="sandbox", table_name=["orders", "events"])
print(tables)
```

[SQL functions index](index.md)
