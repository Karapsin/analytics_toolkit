[SQL functions index](index.md)

# table_info

Inspect whether a table exists and return resolved table and column metadata.

```python
table_info(db_key: 'str', table: 'str', include_row_count: 'bool' = False) -> 'SqlTableInfo'
```

## Inputs

- `db_key`: Connection key or alias from `.connections`.
- `table`: Table name to inspect, modify, or use for partition operations.
- `include_row_count`: Whether table inspection should run a row-count query.

[SQL functions index](index.md)
