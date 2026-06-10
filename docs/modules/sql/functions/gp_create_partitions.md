[SQL functions index](index.md)

# gp_create_partitions

Create multiple Greenplum range or list partitions in input order.

```python
gp_create_partitions(db_key: 'str', table: 'str', *, intervals: 'Sequence[Mapping[str, Any]] | None' = None, values: 'Sequence[str] | None' = None, days: 'Sequence[str] | None' = None, weeks: 'Sequence[str] | None' = None, months: 'Sequence[str] | None' = None, years: 'Sequence[str] | None' = None, name_template: 'str' = 'p_{}', retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, only_generate_sql: 'bool' = False, return_metadata: 'bool' = False) -> 'str | SqlPlan | SqlOperationResult | None'
```

## Inputs

- `db_key` - connection key or alias from `.connections`
- `table` - table name to inspect, modify, or use for partition operations
- `retry_cnt` - number of operation retries with fresh connections
- `timeout_increment` - delay increment used between operation retries
- `dry_run` - when `True`, return a plan without mutating the database
- `return_sql` - when `True`, return a `SqlPlan` instead of mutating a database
- `only_generate_sql` - when `True`, return generated partition DDL as a formatted string
- `return_metadata` - when `True`, return `SqlOperationResult` instead of the historical bare value
- `query_label` - safe label added to generated SQL comments, plans, metadata, and logs
- `intervals` - explicit Greenplum partition interval definitions
- `values` - list partition values used to create Greenplum list partitions
- `days` - day values used to create Greenplum range partitions
- `weeks` - week-start Monday values used to create Greenplum range partitions
- `months` - month-start values used to create Greenplum range partitions
- `years` - year-start January 1 values used to create Greenplum range partitions
- `name_template` - template used to build generated Greenplum partition names

## Usage

```python
from analytics_toolkit import sql

sql.gp_create_partitions(
    "gp",
    "sandbox.events",
    days=["2026-06-01", "2026-06-02"],
)
```

```python
from analytics_toolkit import sql

ddl = sql.gp_create_partitions(
    "gp",
    "sandbox.events",
    days=["2026-06-01", "2026-06-02"],
    only_generate_sql=True,
)
```

Output example:

```python
ddl
# 'ALTER TABLE sandbox.events ADD PARTITION p_20260601 ...;'
```

## Notes

- Exactly one of `intervals`, `values`, `days`, `weeks`, `months`, or `years` must be passed.

[SQL functions index](index.md)
