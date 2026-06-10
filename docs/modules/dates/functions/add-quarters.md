[All date functions](index.md)

# add_quarters

Move by whole quarters from the quarter start that contains the input date.

```python
add_quarters(dt, n, output_string=True)
```

## Inputs

- `dt`: Input date as an ISO string, `date`, or `datetime`.
- `n`: Number of quarters to add. Use a negative value to subtract.
- `output_string`: When `True`, return an ISO string; when `False`, return a midnight `datetime`.

## Usage

```python
from analytics_toolkit.dates import add_quarters

next_quarter = add_quarters("2026-04-10", 1)
previous_quarter = add_quarters("2026-04-10", -1)
```

Output example:

```python
next_quarter
# '2026-07-01'

previous_quarter
# '2026-01-01'
```

## Notes

- The calculation starts from the beginning of the input date's quarter.

[All date functions](index.md)
