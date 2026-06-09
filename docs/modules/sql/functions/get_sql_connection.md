[SQL functions index](index.md)

# get_sql_connection

Open a backend connection for a configured connection key.

```python
get_sql_connection(db_key: 'str') -> 'Any'
```

## Inputs

- `db_key`: Connection key or alias from `.connections`, or an Airflow connection ID when Airflow routing is active.

## Usage

```python
from analytics_toolkit import sql

connection = sql.get_sql_connection("gp")
with connection.cursor() as cursor:
    cursor.execute("select 1")
```

[SQL functions index](index.md)
