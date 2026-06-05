[SQL functions index](index.md)

# extract_ddl

Return native `CREATE TABLE` DDL for one table or several tables.

```python
extract_ddl(db_key: 'str', tables: 'str | Sequence[str]') -> 'str'
```

## Inputs

- `db_key`: Connection key or alias from `.connections`.
- `tables`: One table name or a sequence of table names for DDL extraction.

[SQL functions index](index.md)
