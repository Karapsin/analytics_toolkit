[SQL functions index](index.md)

# with_sql_connection

Decorate a function so it receives a managed SQL connection.

```python
with_sql_connection(db_key: 'str') -> 'Callable[..., Any]'
```

## Inputs

- `db_key`: Connection key or alias from `.connections`, or an Airflow connection ID when Airflow routing is active.

## Usage

```python
from analytics_toolkit import sql

@sql.with_sql_connection("gp")
def read_one(connection):
    with connection.cursor() as cursor:
        cursor.execute("select 1")
        return cursor.fetchone()[0]

value = read_one()
```

## Notes

- The decorated function receives a managed connection object.

[SQL functions index](index.md)
