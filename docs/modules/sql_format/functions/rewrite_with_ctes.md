[Functions index](index.md)

# rewrite_with_ctes

Rewrite supported derived-table SELECT subqueries into named CTEs.

```python
rewrite_with_ctes(sql, *, dialect=None, strategy="auto", cte_prefix="cte", group_by_format="ordinal", order_by_format="ordinal", keyword_case="lower", indent=4, cte_blank_lines=1, union_blank_lines=1) -> str
```

## Inputs

- `sql` - SQL text containing exactly one SELECT statement
- `dialect` - optional sqlglot dialect; use `postgres`, `trino`, `clickhouse`, or `None`
- `strategy` - rewrite strategy; `auto` is the supported v1 strategy
- `cte_prefix` - prefix for generated CTE names such as `cte_1`
- `group_by_format` - `ordinal` uses SELECT-list positions when a GROUP BY item can be matched; `expressions` preserves expression-based output
- `order_by_format` - `ordinal` uses SELECT-list positions when an ORDER BY item can be matched and preserves sort modifiers; `expressions` preserves expression-based output
- `keyword_case` - keyword case: `upper`, `lower`, or `capitalize`
- `indent` - number of spaces per indentation level
- `cte_blank_lines` - number of empty lines between adjacent CTE definitions
- `union_blank_lines` - number of empty lines before `UNION` and `UNION ALL`

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
- Grouping and sorting clauses use compact ordinals by default; pass `"expressions"` modes to preserve expression output

[Functions index](index.md)
