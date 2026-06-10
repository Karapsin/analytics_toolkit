[SQL functions index](index.md)

# gp_vacuum

Run Greenplum `VACUUM` outside a transaction block.

```python
gp_vacuum(table_name: 'str', analyze: 'bool' = False, full: 'bool' = False, verbose: 'bool' = True, db_key: 'str' = 'gp') -> 'None'
```

## Inputs

- `db_key` - connection key or alias from `.connections`, or an Airflow connection ID when Airflow routing is active
- `table_name` - target or source table name, depending on the helper
- `analyze` - whether to include `ANALYZE` in the Greenplum vacuum command
- `full` - whether Greenplum vacuum should use `FULL`
- `verbose` - whether Greenplum vacuum should include `VERBOSE`

## Usage

```python
from analytics_toolkit import sql

sql.gp_vacuum("sandbox.orders", analyze=True)
```

Output example:

```python
# Returns None after VACUUM finishes.
```

## Notes

- Runs outside a transaction block.

[SQL functions index](index.md)
