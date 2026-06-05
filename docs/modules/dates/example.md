[Date helpers index](index.md)

# Reporting Date Windows

Reporting workflows often need the same sequence: normalize a boundary date,
build a period range, offset it for adjacent reporting windows, and format a
partition value for SQL or file paths.

```python
from analytics_toolkit.dates import (
    add_months,
    first_day,
    gen_dates_list,
    sanitize_date,
)

report_month = first_day("2026-04-10")
previous_month = add_months(report_month, -1)
days = gen_dates_list(previous_month, report_month)
partition_value = sanitize_date(report_month)
```

Inputs for [first_day](functions/first-day.md),
[add_months](functions/add-months.md),
[gen_dates_list](functions/gen-dates-list.md), and
[sanitize_date](functions/sanitize-date.md) accept ISO date strings, `date`, or
`datetime` values. Weekly, monthly, and quarterly sequences are truncated to
period starts and warn when either bound is adjusted.

Use [last_day](functions/last-day.md) when a report needs a period end instead
of a period start. Use [add_days](functions/add-days.md),
[add_weeks](functions/add-weeks.md), or
[add_quarters](functions/add-quarters.md) for adjacent periods. Use
[get_today](functions/get-today.md) for the current date and
[get_random_day](functions/get-random-day.md) for random date sampling.

[Date helpers index](index.md)
