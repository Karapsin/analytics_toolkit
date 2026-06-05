[SQL functions index](index.md)

# with_sql_connection

Decorate a function so it receives a managed SQL connection.

```python
with_sql_connection(connection_key: 'str') -> 'Callable[..., Any]'
```

## Inputs

- `connection_key`: Connection key or alias from `.connections`, or an Airflow connection ID when Airflow routing is active.

## Notes

- The decorated function receives a managed connection object.

[SQL functions index](index.md)
