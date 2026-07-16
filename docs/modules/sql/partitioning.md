[SQL module index](index.md)

# Partitioning

Partition workflows cover two separate jobs: creating partition structures and
removing partition values. The exact SQL differs by backend, so the important
concept is the table shape being managed. Use
[sql.gp_create_partitions](functions/gp_create_partitions.md) for
Greenplum creation and
[sql.drop_partitions](functions/drop_partitions.md) for removal.

## Greenplum Initial Creation

When [sql.create_sql_table](functions/create_sql_table.md),
[sql.load_df](functions/load_df.md), or [sql.transfer](functions/transfer.md)
creates a Greenplum target, pass one `partition_by` column and `gp_partitions`.
Range definitions use inclusive `start`, exclusive `end`, and an aligned
positive day, week, month, or year interval:

```python
gp_partitions={
    "start": "2025-01-01",
    "end": "2026-07-01",
    "interval": "1 month",
}
```

List definitions create one `p_<sanitized_value>` child per value:

```python
gp_partitions={"values": ["free", "paid"]}
```

Initial definitions are creation-only. Passing them for an existing target is
validated but does not alter that target, and normal staging tables remain
unpartitioned. No default partition is generated.

## Greenplum Later Partitions

Greenplum partition creation supports explicit intervals, list values, days,
weeks, months, or years. Range partition boundaries are generated in input
order. Week inputs should be Mondays, month inputs first-of-month dates, and
year inputs January 1 dates.

Render partition DDL with
[sql.gp_create_partitions](functions/gp_create_partitions.md) and
`only_generate_sql=True` when reviewing table changes or preparing a SQL change
script.

## Partition Removal

Greenplum removes partitions with native partition DDL and can truncate instead
of dropping when the table shape should remain.

Trino uses Iceberg-style deletes for partition values, so a partition column is
required.

ClickHouse drops partitions from the managed shard table for distributed/shard
targets.

[SQL module index](index.md)
