[SQL module index](index.md)

# Table Creation

Table creation can start from an in-memory dataframe or from a source SQL query.
Choose dataframe-based creation when Python owns the rows. Choose query-based
creation when database metadata should define the target columns.

## DataFrame-Based Creation

Dataframe-based creation infers backend-native column types from pandas data.
This works well for straightforward numeric, string, date, and timestamp data.
Use `table_schema` when precision, scale, nullability, or binary/text handling
must be explicit.

## Query-Based Creation

Query-based creation reads the source query's native column metadata and maps it
to the target backend. It can create an empty target table or create and insert
the query result. Cross-backend inserts delegate to the transfer workflow after
the target is created.

## Backend Shape

Greenplum created tables default to append-only column-oriented storage and
random distribution unless a distribution key is provided.

Trino created tables use Parquet-oriented table options and can include Iceberg
partitioning and sorting options when provided.

ClickHouse distributed targets create a local shard table plus a distributed
table. Local-only targets should opt into shard-only behavior explicitly.

[SQL module index](index.md)
