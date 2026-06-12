[Functions index](index.md)

# format_sql

Format exactly one SQL statement without opening a database connection.

```python
format_sql(sql, *, dialect=None, leading_commas=False, where_anchor="1=1", keyword_case="lower", indent=4) -> str
```

## Inputs

- `sql` - SQL text containing exactly one statement
- `dialect` - optional sqlglot dialect; use `postgres`, `trino`, `clickhouse`, or `None`
- `leading_commas` - whether projection and grouping lists should use leading commas
- `where_anchor` - WHERE normalization mode: `1=1`, `true`, `first_condition`, or `preserve`
- `keyword_case` - keyword case: `upper`, `lower`, or `capitalize`
- `indent` - number of spaces per indentation level

## Usage

```python
from analytics_toolkit import sql_format

formatted = sql_format.format_sql(
    "select user_id, amount from orders where amount > 100",
    dialect="postgres",
)
print(formatted)
```

Output example:

```sql
select
    user_id,
    amount
from orders
where 1=1
      and amount > 100
```

## Notes

- Empty SQL, multi-statement SQL, and SQL that cannot be parsed are rejected with `ValueError`
- Trailing semicolons are preserved only when the single input statement ends with one
- `where_anchor="preserve"` leaves parsed WHERE conditions unchanged instead of adding or removing anchors

[Functions index](index.md)
