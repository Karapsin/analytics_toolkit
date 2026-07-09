[Functions index](index.md)

# gen_datetimes_list

Build a second, minute, hour, day, week, month, or quarter timestamp sequence.

```python
gen_datetimes_list(start_dttm, end_dttm, interval="hours", output_string=True)
```

## Inputs

- `start_dttm` - first timestamp in the requested range as an ISO string, `date`, or timezone-naive `datetime`
- `end_dttm` - last timestamp in the requested range as an ISO string, `date`, or timezone-naive `datetime`
- `interval` - `"second"`/`"seconds"`, `"minute"`/`"minutes"`, `"hour"`/`"hours"`, `"day"`/`"days"`, `"week"`/`"weeks"`, `"month"`/`"months"`, or `"quarter"`/`"quarters"`
- `output_string` - when `True`, return `YYYY-MM-DD HH:MM:SS` strings; when `False`, return `datetime` values

## Usage

```python
from analytics_toolkit import datetime as dttm

hours = dttm.gen_datetimes_list(
    "2026-01-01 12:13:15",
    "2026-01-01 14:13:15",
)
months = dttm.gen_datetimes_list(
    "2026-01-31 12:13:15",
    "2026-03-31 12:13:15",
    interval="months",
)
```

Output example:

```python
hours
# ['2026-01-01 12:13:15', '2026-01-01 13:13:15', '2026-01-01 14:13:15']

months
# ['2026-01-31 12:13:15', '2026-02-28 12:13:15', '2026-03-28 12:13:15']
```

[Functions index](index.md)
