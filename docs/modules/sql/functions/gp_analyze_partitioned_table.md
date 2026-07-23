[SQL functions index](index.md)
[SQL module index](../index.md)

# gp_analyze_partitioned_table

Analyze Greenplum leaf partitions independently.

```python
gp_analyze_partitioned_table(db_key: 'str', partition_names: 'str | Sequence[str] | None' = None, *, concurrency: 'int' = 1, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False) -> 'SqlPlan | SqlOperationResult | None'
```

## Inputs

- `db_key` - Greenplum connection key or alias from `.connections`
- `partition_names` - one schema-qualified physical leaf partition name or a sequence of names; when omitted, discover every visible leaf partition
- `concurrency` - maximum number of independent partition analyses to run at once
- `retry_cnt` - number of retries for each partition with a fresh connection
- `timeout_increment` - delay increment used between retries
- `query_label` - safe label added to generated SQL comments, plans, metadata, and logs
- `dry_run` - when `True`, return a plan without running `ANALYZE`
- `return_sql` - when `True`, return a `SqlPlan` instead of running `ANALYZE`
- `return_metadata` - when `True`, return `SqlOperationResult` with the execution plan and metadata

## Usage

```python
from analytics_toolkit import sql

sql.gp_analyze_partitioned_table(
    "gp",
    ["reporting.events_1_prt_2026_01", "reporting.events_1_prt_2026_02"],
    concurrency=2,
)
```

```python
plan = sql.gp_analyze_partitioned_table("gp", dry_run=True)

plan.sqls
# ['ANALYZE "reporting"."events_1_prt_2026_01"', ...]
```

## Notes

- Automatic discovery reads Greenplum catalog metadata even for a dry run so the plan contains the resolved partitions.
- Each partition is analyzed with a separate connection. On failure, no additional partitions are scheduled; already-running analyses finish their normal cleanup before the error is raised.

[SQL functions index](index.md)
