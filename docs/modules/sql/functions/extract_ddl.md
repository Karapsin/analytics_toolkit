[SQL functions index](index.md)

# extract_ddl

Return native `CREATE TABLE` DDL for one table or several tables.

```python
extract_ddl(db_key: 'str', tables: 'str | Sequence[str]') -> 'str'
```

## Inputs

- `db_key` - connection key or alias from `.connections`
- `tables` - one table name or a sequence of table names for DDL extraction

## Backend Notes

Greenplum uses the native `pg_get_tabledef` helper when the cluster provides it.
On clusters without that helper, `extract_ddl` reconstructs schema DDL from
catalog metadata, including columns, constraints, storage options, distribution,
indexes, partitions where discoverable, and comments. Ownership and grants are
not included.

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
