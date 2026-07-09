[All date functions](index.md)

# is_greater

Return whether one date is after another date.

```python
is_greater(dt, other_dt, inclusive=False)
```

## Inputs

- `dt` - date to compare as an ISO string, `date`, or `datetime`
- `other_dt` - date to compare against as an ISO string, `date`, or `datetime`
- `inclusive` - when `True`, equality is accepted

## Usage

```python
from analytics_toolkit.dates import is_greater

after_start = is_greater("2026-05-19", "2026-05-18")
same_or_after_start = is_greater("2026-05-18", "2026-05-18", inclusive=True)
```

Output example:

```python
after_start
# True

same_or_after_start
# True
```

## Notes

- With `inclusive=False`, equal dates return `False`.
- `datetime` inputs are compared by calendar date and ignore the time component.

[All date functions](index.md)
