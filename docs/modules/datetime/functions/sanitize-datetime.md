[Functions index](index.md)

# sanitize_datetime

Format a timestamp as compact `YYYYMMDDHHMMSS` text.

```python
sanitize_datetime(dt)
```

## Inputs

- `dt` - input timestamp as an ISO string, `date`, or timezone-naive `datetime`

## Usage

```python
from analytics_toolkit import datetime as dttm

partition_suffix = dttm.sanitize_datetime("2026-01-01 12:13:15")
```

Output example:

```python
partition_suffix
# '20260101121315'
```

[Functions index](index.md)
