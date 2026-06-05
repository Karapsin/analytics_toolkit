[Date helpers index](index.md)

# Example

```python
from analytics_toolkit.dates.dates import first_day, gen_dates_list, sanitize_date

first_day("2026-04-10")
gen_dates_list("2026-04-01", "2026-04-10")
gen_dates_list("2026-01-15", "2026-10-20", interval="quarters")
sanitize_date("2026-05-18")
```

Inputs accept ISO date strings, `date`, or `datetime` values.
Weekly, monthly, and quarterly sequences are truncated to period starts and warn
when either bound is adjusted.

[Date helpers index](index.md)
