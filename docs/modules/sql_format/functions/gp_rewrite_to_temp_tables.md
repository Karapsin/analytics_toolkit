[Functions index](index.md)

# gp_rewrite_to_temp_tables

Rewrite SELECT CTEs and subqueries into a Greenplum temp-table script.

```python
gp_rewrite_to_temp_tables(sql, *, dialect="postgres", temp_prefix="tmp", keyword_case="lower", indent=4) -> str
```

## Inputs

- `sql` - SQL text containing exactly one SELECT statement
- `dialect` - optional sqlglot dialect; use `postgres`, `trino`, `clickhouse`, or `None`
- `temp_prefix` - prefix for generated temp table names such as `tmp_1`
- `keyword_case` - keyword case: `upper`, `lower`, or `capitalize`
- `indent` - number of spaces per indentation level

## Usage

```python
from analytics_toolkit import sql_format

script = sql_format.gp_rewrite_to_temp_tables(
    "with customer_orders as ("
    "select user_id, sum(amount) as revenue from orders group by user_id"
    ") "
    "select u.id, customer_orders.revenue "
    "from customer_orders join users u "
    "on customer_orders.user_id = u.id"
)
print(script)
```

Output example:

```sql
drop table if exists customer_orders;

create temporary table customer_orders as (
    select
        user_id,
        SUM(amount) as revenue
    from orders
    group by
        user_id
) distributed by (user_id);
analyze customer_orders;

select
    u.id,
    customer_orders.revenue
from customer_orders
join users as u
    on customer_orders.user_id = u.id
```

## Notes

- CTEs use their CTE alias as the temp table name
- Derived-table subqueries use their table alias as the temp table name
- Scalar and predicate SELECT subqueries use generated names such as `tmp_1`
- Temp tables use `distributed by` when equality join keys can be inferred, otherwise `distributed randomly`
- Empty SQL, multi-statement SQL, non-SELECT SQL, invalid prefixes, name collisions, correlated subqueries, and unsafe partial rewrites raise `ValueError`

[Functions index](index.md)
