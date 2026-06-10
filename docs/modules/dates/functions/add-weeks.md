[All date functions](index.md)

# add_weeks

Move by whole weeks from the week start that contains the input date.

```python
add_weeks(dt, n, output_string=True)
```

## Inputs

- `dt` - input date as an ISO string, `date`, or `datetime`
- `n` - number of weeks to add. Use a negative value to subtract
- `output_string` - when `True`, return an ISO string; when `False`, return a midnight `datetime`

## Usage

```python
from analytics_toolkit.dates import add_weeks

next_week = add_weeks("2026-04-10", 1)
previous_week = add_weeks("2026-04-10", -1)
```

Output example:

```python
next_week
# '2026-04-13'

previous_week
# '2026-03-30'
```

## Notes

- The calculation starts from the beginning of the input date's week.

[All date functions](index.md)
