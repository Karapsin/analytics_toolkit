[All date functions](index.md)

# get_random_day

Return a random date in an inclusive date range.

```python
get_random_day(start_dt, end_dt, output_string=True)
```

## Inputs

- `start_dt` - earliest possible date as an ISO string, `date`, or `datetime`
- `end_dt` - latest possible date as an ISO string, `date`, or `datetime`
- `output_string` - when `True`, return an ISO string; when `False`, return a midnight `datetime`

## Usage

```python
from analytics_toolkit.dates import get_random_day

sample_day = get_random_day("2026-04-01", "2026-04-30")
```

Output example:

```python
sample_day
# '2026-04-18'
```

## Notes

- The range is inclusive.
- `end_dt` earlier than `start_dt` raises `ValueError`.

[All date functions](index.md)
