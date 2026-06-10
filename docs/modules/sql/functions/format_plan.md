[SQL functions index](index.md)

# format_plan

Render a compact readable summary of a dry-run `SqlPlan`.

```python
format_plan(plan: 'SqlPlan', *, include_sql: 'bool' = True, max_sql_chars: 'int' = 160) -> 'str'
```

## Inputs

- `plan` - `SqlPlan` returned by a dry-run or `return_sql=True` call
- `include_sql` - whether formatted plan output should include SQL text
- `max_sql_chars` - maximum SQL text length to include per statement in formatted plan output

## Usage

```python
from analytics_toolkit import sql

plan = sql.transfer(
    from_db="trino",
    to_db="gp",
    from_sql="select * from sandbox.orders",
    to_table="sandbox.orders_copy",
    dry_run=True,
)
print(sql.format_plan(plan, max_sql_chars=120))
```

Output example:

```text
SQL plan: transfer
- trino: select * from sandbox.orders
- gp: CREATE TABLE sandbox.orders_copy (...)
```

[SQL functions index](index.md)
