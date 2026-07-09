[Functions index](index.md)

# is_period_start

Check whether a timestamp is exactly the start of a period.

```python
is_period_start(dt, period="day")
```

## Inputs

- `dt` - input timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `period` - period to use: `"minute"`, `"hour"`, `"day"`, `"week"`, `"month"`, or `"quarter"`

## Usage

```python
from analytics_toolkit import datetime as dttm

starts_hour = dttm.is_period_start("2026-05-18 12:00:00", period="hour")
```

Output example:

```python
starts_hour
# True
```

[Functions index](index.md)
