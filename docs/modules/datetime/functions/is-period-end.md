[Functions index](index.md)

# is_period_end

Check whether a timestamp is exactly the end of a period.

```python
is_period_end(dt, period="day")
```

## Inputs

- `dt` - input timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `period` - period to use: `"minute"`, `"hour"`, `"day"`, `"week"`, `"month"`, or `"quarter"`

## Usage

```python
from analytics_toolkit import datetime as dttm

ends_hour = dttm.is_period_end("2026-05-18 12:59:59", period="hour")
```

Output example:

```python
ends_hour
# True
```

[Functions index](index.md)
