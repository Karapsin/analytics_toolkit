[SQL functions index](index.md)

# airflow_connection_config

Build toolkit connection config from one Airflow Connection.

```python
airflow_connection_config(connection_id: 'str', backend: 'BackendName | str | None' = None) -> 'ConnectionConfig'
```

## Inputs

### General Inputs

- `connection_id`: Airflow connection ID to read.
- `backend`: Backend name to use for an Airflow Connection; when omitted, infer it from Airflow metadata.

### Backend-Specific Inputs

None.

## Usage

```python
from analytics_toolkit import sql

config = sql.airflow_connection_config("airflow_trino", backend="trino")
print(config.type)
```

[SQL functions index](index.md)
