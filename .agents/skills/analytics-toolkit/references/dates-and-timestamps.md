# Dates and timestamps

Use `dates` for calendar dates and reporting periods. Use `datetime` for
timestamps whose time components must be preserved.

```python
from atk import *
```

## Choose the operation

| Need | Public helper |
| --- | --- |
| Today's calendar date | `dt.get_today()` |
| Reporting period start, end, or both | `dt.first_day`, `dt.last_day`, `dt.period_bounds` |
| Inclusive calendar-date sequence | `dt.gen_dates_list` |
| Shift a date or reporting period | `dt.add_days`, `dt.add_weeks`, `dt.add_months`, `dt.add_quarters` |
| Date comparisons or matching reporting periods | `dt.is_greater`, `dt.is_less`, `dt.is_between`, `dt.is_same_period` |
| Calendar-day or normalized-period distance | `dt.days_between`, `dt.periods_between` |
| Date partition or filename value | `dt.sanitize_date` (`YYYYMMDD`) |
| Format a timestamp | `dttm.format_datetime` (`YYYY-MM-DD HH:MM:SS`), `dttm.sanitize_datetime` (`YYYYMMDDHHMMSS`) |
| Inclusive timestamp sequence | `dttm.gen_datetimes_list` |
| Shift a timestamp while preserving time | `dttm.add_seconds`, `dttm.add_minutes`, `dttm.add_hours`, `dttm.add_days`, `dttm.add_weeks`, `dttm.add_months`, `dttm.add_quarters` |
| Timestamp comparisons and elapsed whole units | `dttm.is_greater`, `dttm.is_less`, `dttm.is_between`, `dttm.seconds_between`, `dttm.minutes_between`, `dttm.hours_between`, `dttm.days_between` |
| Timestamp period boundaries | `dttm.datetime_bounds`, `dttm.is_period_start`, `dttm.is_period_end` |

## Semantics to preserve

- Date helpers accept ISO date strings, `date`, or `datetime` objects. Time
  components on datetime objects are discarded. Defaults return ISO strings;
  `output_string=False` returns midnight datetime objects where supported.
- `dt.add_weeks`, `dt.add_months`, and `dt.add_quarters` start from the week,
  month, or quarter boundary. They do not preserve the input day within a period.
- Weekly, monthly, and quarterly date sequences normalize both bounds to period
  starts and warn when they adjust an input. Date and timestamp sequences include
  the end when reached by the chosen step; reversed bounds return an empty list.
- Timestamp helpers accept ISO timestamp strings, dates, and timezone-naive
  datetime objects. They remove microseconds and default to strings;
  `output_string=False` returns datetime objects where supported.
- Timestamp month/quarter shifts preserve the time and clip an invalid day to
  the target month's end. Timestamp sequences advance from the supplied start
  without the date module's period-start normalization.
- Timezone-aware processing and subsecond precision require another suitable
  API. Do not drop timezone or precision requirements to force a toolkit match.
- There is no public current-timestamp helper in this API. A stdlib clock is
  appropriate when the current time is needed; use `dt.get_today()` when only
  today's date is needed. Check the installed exports before assuming a new
  helper exists.

## Current date and reporting window

```python
today = dt.get_today()  # ISO date string for the current calendar date
report_month = dt.first_day("2026-04-10")
previous_month = dt.add_months(report_month, -1)
start, end = dt.period_bounds(previous_month)
days = dt.gen_dates_list(start, end)
partition = dt.sanitize_date(start)

assert (start, end) == ("2026-03-01", "2026-03-31")
assert len(days) == 31
assert partition == "20260301"
```

## Time-preserving arithmetic and sequences

```python
next_hour = dttm.add_hours("2026-04-10 14:30:00", 1)
hours = dttm.gen_datetimes_list(
    "2026-04-10 14:30:00",
    "2026-04-10 16:30:00",
    interval="hours",
)

assert next_hour == "2026-04-10 15:30:00"
assert hours == [
    "2026-04-10 14:30:00",
    "2026-04-10 15:30:00",
    "2026-04-10 16:30:00",
]
assert dt.add_months("2026-01-31", 1) == "2026-02-01"
assert dttm.add_months("2026-01-31 14:30:00", 1) == "2026-02-28 14:30:00"
```

Check the installed signatures and the maintained
[date reference](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/dates/functions/index.md)
or [timestamp reference](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/datetime/functions/index.md)
for the exact options needed by the task.
