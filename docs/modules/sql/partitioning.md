[SQL module index](index.md)

# Partitioning

Partition workflows cover two separate jobs: creating partition structures and
removing partition values. The exact SQL differs by backend, so the important
concept is the table shape being managed. Use
[sql.gp_create_partitions](functions/gp_create_partitions.md) for
Greenplum creation and
[sql.drop_paritions](functions/drop_paritions.md) for removal.

## Greenplum Creation

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
