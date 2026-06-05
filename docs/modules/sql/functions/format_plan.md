[SQL functions index](index.md)

# format_plan

Render a compact readable summary of a dry-run `SqlPlan`.

```python
format_plan(plan: 'SqlPlan', *, include_sql: 'bool' = True, max_sql_chars: 'int' = 160) -> 'str'
```

## Inputs

- `plan`: `SqlPlan` returned by a dry-run or `return_sql=True` call.
- `include_sql`: Whether formatted plan output should include SQL text.
- `max_sql_chars`: Maximum SQL text length to include per statement in formatted plan output.

[SQL functions index](index.md)
