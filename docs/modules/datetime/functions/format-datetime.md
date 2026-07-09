[Functions index](index.md)

# format_datetime

Format a timestamp as `YYYY-MM-DD HH:MM:SS`.

```python
format_datetime(dt)
```

## Inputs

- `dt` - input timestamp as an ISO string, `date`, or timezone-naive `datetime`

## Usage

```python
from analytics_toolkit import datetime as dttm

formatted = dttm.format_datetime("2026-01-01T12:13:15")
```

Output example:

```python
formatted
# '2026-01-01 12:13:15'
```

[Functions index](index.md)
