[All date functions](index.md)

# sanitize_date

Convert a date to `YYYYMMDD`.

```python
sanitize_date(dt)
```

## Inputs

- `dt` - input date as an ISO string, `date`, or `datetime`

## Usage

```python
from analytics_toolkit.dates import sanitize_date

partition_value = sanitize_date("2026-05-18")
```

Output example:

```python
partition_value
# '20260518'
```

[All date functions](index.md)
