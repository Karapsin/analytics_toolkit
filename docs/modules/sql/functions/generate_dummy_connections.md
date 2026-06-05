[SQL functions index](index.md)

# generate_dummy_connections

Create a starter `.connections` file in the current working directory.

```python
generate_dummy_connections(airflow: 'bool' = False) -> 'Path'
```

## Inputs

- `airflow`: When `True`, generate Airflow routing metadata instead of direct connection placeholders.

## Notes

- The helper writes `./.connections` only when it does not already exist; an existing file raises `ValueError`.

[SQL functions index](index.md)
