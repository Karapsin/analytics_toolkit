[SQL functions index](index.md)

# gp_vacuum

Run Greenplum `VACUUM` outside a transaction block.

```python
gp_vacuum(table_name: 'str', analyze: 'bool' = False, full: 'bool' = False, verbose: 'bool' = True, connection_key: 'str' = 'gp') -> 'None'
```

## Inputs

### General Inputs

- `connection_key`: Connection key or alias from `.connections`, or an Airflow connection ID when Airflow routing is active.
- `table_name`: Target or source table name, depending on the helper.

### Backend-Specific Inputs

- `analyze`: Whether to include `ANALYZE` in the Greenplum vacuum command.
- `full`: Whether Greenplum vacuum should use `FULL`.
- `verbose`: Whether Greenplum vacuum should include `VERBOSE`.

## Usage

```python
from analytics_toolkit import sql

sql.gp_vacuum("sandbox.orders", analyze=True)
```

## Notes

- Runs outside a transaction block.

[SQL functions index](index.md)
