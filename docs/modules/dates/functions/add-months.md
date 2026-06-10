[All date functions](index.md)

# add_months

Move by whole months from the month start that contains the input date.

```python
add_months(dt, n, output_string=True)
```

## Inputs

- `dt`: Input date as an ISO string, `date`, or `datetime`.
- `n`: Number of months to add. Use a negative value to subtract.
- `output_string`: When `True`, return an ISO string; when `False`, return a midnight `datetime`.

## Usage

```python
from analytics_toolkit.dates import add_months

next_month = add_months("2026-04-10", 1)
previous_month = add_months("2026-04-10", -1)
```

Output example:

```python
next_month
# '2026-05-01'

previous_month
# '2026-03-01'
```

## Notes

- The calculation starts from the beginning of the input date's month.

[All date functions](index.md)
