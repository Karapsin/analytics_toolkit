[Module index](index.md)

# Formatting and CTE Rewrites

`analytics_toolkit.sql_format` is for local SQL text transformations. It is
separate from `analytics_toolkit.sql`, which handles configured database reads,
writes, transfers, and metadata operations.

Use [format_sql](functions/format_sql.md) when SQL should be normalized before
review, logging, or storage. The formatter accepts one statement, applies stable
indentation, can choose leading or trailing comma style, and can normalize WHERE
clauses with an explicit anchor.

```python
from analytics_toolkit import sql_format

print(
    sql_format.format_sql(
        "select user_id, amount from orders where amount > 100",
        where_anchor="true",
    )
)
```

Output example:

```sql
SELECT
    user_id,
    amount
FROM orders
WHERE
    TRUE AND amount > 100
```

Use [rewrite_with_ctes](functions/rewrite_with_ctes.md) when a derived-table
query should be made easier to read before execution elsewhere. The rewrite is
conservative: unsupported subquery shapes raise `ValueError` instead of
returning a partial rewrite.

[Module index](index.md)
