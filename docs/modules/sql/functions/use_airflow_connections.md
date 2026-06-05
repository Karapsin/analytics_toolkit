[SQL functions index](index.md)

# use_airflow_connections

Temporarily route SQL helpers to Airflow Connections instead of a `.connections` file.

```python
use_airflow_connections(connection_backends: 'Mapping[str, BackendName | str] | None' = None, *, default_backend: 'BackendName | str | None' = None) -> 'Iterator[None]'
```

## Inputs

- `connection_backends`: Optional mapping of Airflow connection IDs to backend names for the temporary routing context.
- `default_backend`: Backend used by Airflow routing when a connection ID is not explicitly mapped.

## Usage

```python
from analytics_toolkit import sql

with sql.use_airflow_connections({"airflow_trino": "trino"}):
    events = sql.read("airflow_trino", "select * from sandbox.events limit 10")
```

## Notes

- This is a context manager for DAG/runtime code that should resolve connection IDs through Airflow.

[SQL functions index](index.md)
