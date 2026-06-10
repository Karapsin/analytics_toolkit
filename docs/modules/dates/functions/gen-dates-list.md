[All date functions](index.md)

# gen_dates_list

Build a daily, weekly, monthly, or quarterly sequence.

```python
gen_dates_list(start_dt, end_dt, interval="days", output_string=True)
```

## Inputs

- `start_dt`: First date in the requested range as an ISO string, `date`, or `datetime`.
- `end_dt`: Last date in the requested range as an ISO string, `date`, or `datetime`.
- `interval`: `"day"`/`"days"`, `"week"`/`"weeks"`, `"month"`/`"months"`, or `"quarter"`/`"quarters"`.
- `output_string`: When `True`, return ISO strings; when `False`, return midnight `datetime` values.

## Usage

```python
from analytics_toolkit.dates import gen_dates_list

days = gen_dates_list("2026-04-01", "2026-04-10")
months = gen_dates_list("2026-01-15", "2026-06-20", interval="months")
```

Output example:

```python
days
# ['2026-04-01', '2026-04-02', ..., '2026-04-10']

months
# ['2026-01-01', '2026-02-01', ..., '2026-06-01']
```

## Notes

- Weekly, monthly, and quarterly ranges are truncated to period starts and warn
  when either bound is adjusted.

[All date functions](index.md)
