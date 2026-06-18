[Module index](index.md)

# Formatting and CTE Rewrites

`analytics_toolkit.sql_format` is for local SQL text transformations. It is
separate from `analytics_toolkit.sql`, which handles configured database reads,
writes, transfers, and metadata operations.

Use [format_sql](functions/format_sql.md) when SQL should be normalized before
review, logging, or storage. The formatter accepts one statement, applies stable
indentation, can choose leading or trailing comma style, and can normalize WHERE
clauses with an explicit anchor. By default, eligible `GROUP BY` and `ORDER BY`
items render as SELECT-list ordinals, and adjacent CTE definitions are separated
by one empty line; pass `group_by_format="expressions"` or
`order_by_format="expressions"` to keep expression-based clause output.

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
select
    user_id,
    amount
from orders
where true
      and amount > 100
```

Use [rewrite_with_ctes](functions/rewrite_with_ctes.md) when a derived-table
query should be made easier to read before execution elsewhere. The rewrite is
conservative: unsupported subquery shapes raise `ValueError` instead of
returning a partial rewrite.

Use [gp_rewrite_to_temp_tables](functions/gp_rewrite_to_temp_tables.md) when a
Greenplum SELECT should be split into explicit temp-table creation, analyze, and
final-query steps. The helper keeps the work local to SQL text: it does not
open a database connection or execute the generated script.

[Module index](index.md)
