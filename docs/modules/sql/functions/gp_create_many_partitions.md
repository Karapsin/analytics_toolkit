[SQL functions index](index.md)

# gp_create_many_partitions

Create multiple Greenplum range or list partitions in input order.

```python
gp_create_many_partitions(db_key: 'str', table: 'str', *, intervals: 'Sequence[Mapping[str, Any]] | None' = None, values: 'Sequence[str] | None' = None, days: 'Sequence[str] | None' = None, weeks: 'Sequence[str] | None' = None, months: 'Sequence[str] | None' = None, years: 'Sequence[str] | None' = None, name_template: 'str' = 'p_{}', retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False) -> 'SqlPlan | SqlOperationResult | None'
```

## Inputs

- `db_key`: Connection key or alias from `.connections`.
- `table`: Table name to inspect, modify, or use for partition operations.
- `intervals`: Explicit Greenplum partition interval definitions.
- `values`: List partition values used to create Greenplum list partitions.
- `days`: Day values used to create Greenplum range partitions.
- `weeks`: Monday week-start values used to create Greenplum range partitions.
- `months`: Month-start values used to create Greenplum range partitions.
- `years`: January 1 year-start values used to create Greenplum range partitions.
- `name_template`: Template used to build generated Greenplum partition names.
- `retry_cnt`: Number of operation retries with fresh connections.
- `timeout_increment`: Delay increment used between operation retries.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.
- `dry_run`: When `True`, return a plan without mutating the database.
- `return_sql`: When `True`, return a `SqlPlan` instead of mutating a database.
- `return_metadata`: When `True`, return `SqlOperationResult` instead of the historical bare value.

## Notes

- Exactly one of `intervals`, `values`, `days`, `weeks`, `months`, or `years` must be passed.

[SQL functions index](index.md)
