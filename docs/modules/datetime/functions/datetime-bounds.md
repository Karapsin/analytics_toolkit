[Functions index](index.md)

# datetime_bounds

Return the start and end timestamps for a period.

```python
datetime_bounds(dt, period="day", output_string=True)
```

## Inputs

- `dt` - input timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `period` - period to use: `"minute"`, `"hour"`, `"day"`, `"week"`, `"month"`, or `"quarter"`
- `output_string` - when `True`, return `YYYY-MM-DD HH:MM:SS` strings; when `False`, return `datetime` values

## Usage

```python
from analytics_toolkit import datetime as dttm

day_window = dttm.datetime_bounds("2026-05-18 12:13:15")
hour_window = dttm.datetime_bounds("2026-05-18 12:13:15", period="hour")
```

Output example:

```python
day_window
# ('2026-05-18 00:00:00', '2026-05-18 23:59:59')

hour_window
# ('2026-05-18 12:00:00', '2026-05-18 12:59:59')
```

[Functions index](index.md)
