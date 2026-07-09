[All date functions](index.md)

# is_between

Return whether a date falls within a date range.

```python
is_between(dt, start_dt, end_dt, inclusive=True)
```

## Inputs

- `dt` - date to check as an ISO string, `date`, or `datetime`
- `start_dt` - lower range bound as an ISO string, `date`, or `datetime`
- `end_dt` - upper range bound as an ISO string, `date`, or `datetime`
- `inclusive` - when `True`, range bounds are accepted

## Usage

```python
from analytics_toolkit.dates import is_between

in_window = is_between("2026-05-18", "2026-05-18", "2026-05-20")
inside_only = is_between("2026-05-19", "2026-05-18", "2026-05-20", inclusive=False)
```

Output example:

```python
in_window
# True

inside_only
# True
```

## Notes

- With `inclusive=False`, dates equal to `start_dt` or `end_dt` return `False`.
- `end_dt` earlier than `start_dt` raises `ValueError`.
- `datetime` inputs are compared by calendar date and ignore the time component.

[All date functions](index.md)
