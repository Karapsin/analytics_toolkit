[SQL functions index](index.md)

# build_gp_create_many_partitions_sqls

Render Greenplum partition DDL without opening a connection.

```python
build_gp_create_many_partitions_sqls(table: 'str', *, intervals: 'Sequence[Mapping[str, Any]] | None' = None, values: 'Sequence[str] | None' = None, days: 'Sequence[str] | None' = None, weeks: 'Sequence[str] | None' = None, months: 'Sequence[str] | None' = None, years: 'Sequence[str] | None' = None, name_template: 'str' = 'p_{}', query_label: 'str | None' = None) -> 'list[str]'
```

## Inputs

- `table`: Table name to inspect, modify, or use for partition operations.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.
- `intervals`: Explicit Greenplum partition interval definitions.
- `values`: List partition values used to create Greenplum list partitions.
- `days`: Day values used to create Greenplum range partitions.
- `weeks`: Monday week-start values used to create Greenplum range partitions.
- `months`: Month-start values used to create Greenplum range partitions.
- `years`: January 1 year-start values used to create Greenplum range partitions.
- `name_template`: Template used to build generated Greenplum partition names.

## Usage

```python
from analytics_toolkit import sql

statements = sql.build_gp_create_many_partitions_sqls(
    "sandbox.events",
    days=["2026-06-01", "2026-06-02"],
)
```

## Notes

- Exactly one of `intervals`, `values`, `days`, `weeks`, `months`, or `years` must be passed.

[SQL functions index](index.md)
