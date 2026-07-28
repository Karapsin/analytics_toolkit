[SQL module index](index.md)

# DataFrame Loading

DataFrame loading is for pushing in-memory pandas data into a configured SQL
target. It is the right workflow when Python already owns the rows and the
target table should be appended, replaced, or refreshed in place. The public
entrypoint is [sql.load_df](functions/load_df.md).

Use `write_mode` when the intended mutation matters:

- `append` keeps existing rows and inserts the dataframe rows.
- `replace` recreates or clears the target using the historical replace path.
- `truncate_insert` keeps the existing table shape, clears rows, then inserts.
- `upsert` stages rows and applies backend-specific replacement semantics.

The older `append` flag still works, but `write_mode` is clearer in shared code.
Do not mix both unless you are preserving compatibility with an existing call.

## Schema and Keys

By default, table creation uses dataframe columns and inferred backend types.
Pass `table_schema` when a column needs a specific backend-native type or when
source data can produce ambiguous pandas dtypes. Use
[sql.create_sql_table](functions/create_sql_table.md) when you need the table
creation step independently from loading rows.

Object columns whose non-null values are all Python `uuid.UUID` instances infer
as native `UUID` on Greenplum, Trino, and ClickHouse. ClickHouse uses
`Nullable(UUID)` when the dataframe column contains nulls. UUID-looking strings,
mixed object columns, and all-null object columns remain string types unless an
explicit `table_schema` selects `UUID`.

`key_columns` lets the load validate staged rows against an existing target
before final insertion. Use it when duplicate keys in append-like flows would be
more expensive to fix after the load finishes.

For `write_mode="upsert"`, `key_columns` is required. Incoming rows are staged
first and duplicate staged keys raise before finalization. If the target table
already exists, its schema is used for final insert column types. If the target
table does not exist, the load creates it from `table_schema` or inferred
dataframe types and inserts the dataframe rows normally.

For Trino and ClickHouse upsert, `upsert_partition_column` is also required and
must name one dataframe column. That column defines the partition replacement
boundary; it is not inferred from `partition_by`, which only controls table
creation shape.

## Backend Notes

Greenplum dataframe inserts use chunked `execute_values` statements. Tune the
insert chunk size for very wide rows or constrained VMEM environments.

Trino sends parameterized multi-row insert statements. A connection-level insert
chunk size can be set in `.connections`, and call-level settings can override
it. Trino upsert builds a final increment for affected partitions and requires
`upsert_partition_drop_sql_template` in `.connections` so connector-specific
partition drop SQL is explicit.

ClickHouse targets normally create and maintain a distributed/shard table pair.
Use `ch_only_shard=True` only when the target should intentionally be a local
ClickHouse table. ClickHouse upsert builds a final increment for affected
partitions, drops those partitions from the shard or local target, and inserts
through the target table.

Greenplum upsert deletes matching keys from the target using the stage table
and then inserts the staged rows.

## Progress and Results

Progress bars are opt-in. Enable them for longer loads where row-level feedback
helps, and keep them off in quiet automation.

Use metadata results when downstream code needs loaded row counts, retry
attempts, elapsed time, or the final target row count.

[SQL module index](index.md)
