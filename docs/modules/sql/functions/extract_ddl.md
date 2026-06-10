[SQL functions index](index.md)

# extract_ddl

Return native `CREATE TABLE` DDL for one table or several tables.

```python
extract_ddl(db_key: 'str', tables: 'str | Sequence[str]') -> 'str'
```

## Inputs

- `db_key` - connection key or alias from `.connections`
- `tables` - one table name or a sequence of table names for DDL extraction

## Usage

```python
from analytics_toolkit import sql

ddl = sql.extract_ddl("trino", ["sandbox.orders", "sandbox.order_summary"])
print(ddl)
```

Output example:

```sql
CREATE TABLE sandbox.orders (...);

CREATE TABLE sandbox.order_summary (...);
```

[SQL functions index](index.md)
