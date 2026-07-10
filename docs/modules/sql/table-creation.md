[SQL module index](index.md)

# Table Creation

Table creation can start from an in-memory dataframe or from a source SQL query.
Choose dataframe-based creation when Python owns the rows. Choose query-based
creation when database metadata should define the target columns. The main
entrypoint is [sql.create_sql_table](functions/create_sql_table.md).

## DataFrame-Based Creation

Dataframe-based creation infers backend-native column types from pandas data.
This works well for straightforward numeric, string, date, and timestamp data.
Use `table_schema` when precision, scale, nullability, or binary/text handling
must be explicit. Use `only_generate_sql=True` to render DDL without executing
it. Use `table_schema` without a dataframe when the column types are already
known.

## Query-Based Creation

Query-based creation reads the source query's native column metadata and maps it
to the target backend. It can create an empty target table or create and insert
the query result. Cross-backend inserts delegate to the transfer workflow after
the target is created with [sql.transfer](functions/transfer.md). If Python
already owns the rows, [sql.load_df](functions/load_df.md) is usually the
simpler workflow.

Each SQL-source retry uses fresh source and target connections and repeats
metadata inspection. A cross-backend insert delegates with one inner transfer
attempt so retries remain bounded by `retry_cnt`. Partial targets owned by a
failed attempt are removed before another attempt; a cleanup failure stops the
workflow rather than risking duplicate rows.

Qualified table names are parsed structurally. Quoted dots remain part of one
identifier, for example `"schema.with.dot"."table.with.dot"`. Unqualified
Greenplum names retain the `public` default; Trino names retain the configured
catalog and schema defaults.

## Backend Shape

Greenplum created tables default to append-only column-oriented storage and
random distribution unless a distribution key is provided.

Trino created tables use Parquet-oriented table options and can include Iceberg
partitioning and sorting options when provided.

ClickHouse distributed targets create a local shard table plus a distributed
table. Local-only targets should opt into shard-only behavior explicitly.

[SQL module index](index.md)
