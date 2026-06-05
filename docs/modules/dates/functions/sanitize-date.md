[All date functions](index.md)

# sanitize_date

Convert a date to `YYYYMMDD`.

```python
sanitize_date(dt)
```

## Inputs

- `dt`: Input date as an ISO string, `date`, or `datetime`.

## Usage

```python
from analytics_toolkit.dates import sanitize_date

partition_value = sanitize_date("2026-05-18")
```

[All date functions](index.md)
