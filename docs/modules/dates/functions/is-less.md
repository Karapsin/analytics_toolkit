[All date functions](index.md)

# is_less

Return whether one date is before another date.

```python
is_less(dt, other_dt, inclusive=False)
```

## Inputs

- `dt` - date to compare as an ISO string, `date`, or `datetime`
- `other_dt` - date to compare against as an ISO string, `date`, or `datetime`
- `inclusive` - when `True`, equality is accepted

## Usage

```python
from analytics_toolkit.dates import is_less

before_end = is_less("2026-05-17", "2026-05-18")
same_or_before_end = is_less("2026-05-18", "2026-05-18", inclusive=True)
```

Output example:

```python
before_end
# True

same_or_before_end
# True
```

## Notes

- With `inclusive=False`, equal dates return `False`.
- `datetime` inputs are compared by calendar date and ignore the time component.

[All date functions](index.md)
