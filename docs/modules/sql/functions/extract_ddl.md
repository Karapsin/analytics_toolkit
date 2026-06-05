[SQL functions index](index.md)

# extract_ddl

Return native `CREATE TABLE` DDL for one table or several tables.

```python
extract_ddl(db_key: 'str', tables: 'str | Sequence[str]') -> 'str'
```

## Inputs

- `db_key`: Connection key or alias from `.connections`.
- `tables`: One table name or a sequence of table names for DDL extraction.

## Usage

```python
from analytics_toolkit import sql

ddl = sql.extract_ddl("trino", ["sandbox.orders", "sandbox.order_summary"])
print(ddl)
```

[SQL functions index](index.md)
