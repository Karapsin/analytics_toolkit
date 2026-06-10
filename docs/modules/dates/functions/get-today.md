[All date functions](index.md)

# get_today

Return today's date.

```python
get_today(output_string=True)
```

## Inputs

- `output_string` - when `True`, return an ISO string; when `False`, return a midnight `datetime`

## Usage

```python
from analytics_toolkit.dates import get_today

today = get_today()
```

Output example:

```python
today
# '2026-06-10'
```

[All date functions](index.md)
