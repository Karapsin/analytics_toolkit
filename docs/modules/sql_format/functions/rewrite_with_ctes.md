[Functions index](index.md)

# rewrite_with_ctes

Rewrite supported derived-table SELECT subqueries into named CTEs.

```python
rewrite_with_ctes(sql, *, dialect=None, strategy="auto", cte_prefix="cte", keyword_case="lower", indent=4) -> str
```

## Inputs

- `sql` - SQL text containing exactly one SELECT statement
- `dialect` - optional sqlglot dialect; use `postgres`, `trino`, `clickhouse`, or `None`
- `strategy` - rewrite strategy; `auto` is the supported v1 strategy
- `cte_prefix` - prefix for generated CTE names such as `cte_1`
- `keyword_case` - keyword case: `upper`, `lower`, or `capitalize`
- `indent` - number of spaces per indentation level

## Usage

```python
from analytics_toolkit import sql_format

rewritten = sql_format.rewrite_with_ctes(
    "select s.user_id from (select user_id from orders) s"
)
print(rewritten)
```

Output example:

```sql
with cte_1 as (
    select
        user_id
    from orders
)
select
    s.user_id
from cte_1 as s
```

## Notes

- v1 extracts SELECT subqueries in `FROM` and `JOIN` positions only
- Queries with scalar subqueries, unsupported nested derived subqueries, or no extractable subquery raise `ValueError`
- Predicate, join, projection, grouping, sorting, and limit order are preserved by the AST rewrite

[Functions index](index.md)
