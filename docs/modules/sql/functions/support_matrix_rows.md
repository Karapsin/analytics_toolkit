[SQL functions index](index.md)

# support_matrix_rows

Return backend support matrix rows as dictionaries.

```python
support_matrix_rows() -> 'list[dict[str, str]]'
```

## Inputs

No inputs.

## Usage

```python
from analytics_toolkit import sql

for row in sql.support_matrix_rows():
    print(row["backend"], row["write_modes"])
```

[SQL functions index](index.md)
