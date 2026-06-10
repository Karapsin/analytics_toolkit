[SQL functions index](index.md)

# generate_dummy_connections

Create a starter `.connections` file and `.certs/` directory in the current
working directory.

```python
generate_dummy_connections(airflow: 'bool' = False) -> 'Path'
```

## Inputs

- `airflow`: When `True`, generate Airflow routing metadata instead of direct connection placeholders.

## Usage

```python
from analytics_toolkit import sql

path = sql.generate_dummy_connections()
# For Airflow DAG routing metadata:
# sql.generate_dummy_connections(airflow=True)
```

Output example:

```python
path
# Path('.connections')
```

## Notes

- The helper writes `./.connections` only when it does not already exist; an existing file raises `ValueError`.
- The helper creates `./.certs/` for local certificate files and prints short certificate setup notes for Greenplum, Trino, and ClickHouse.
- Generated connection placeholders include example `ca_certs` values. Replace them with real certificate file names, lists of file names, or remove them if your backend does not require custom CA certificates.

[SQL functions index](index.md)
