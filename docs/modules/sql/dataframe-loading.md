[SQL module index](index.md)

# DataFrame Loading

DataFrame loading is for pushing in-memory pandas data into a configured SQL
target. It is the right workflow when Python already owns the rows and the
target table should be appended, replaced, or refreshed in place. The public
entrypoint is [sql.load_df](functions/load_df.md).

Use `write_mode` when the intended mutation matters:

- `append` keeps existing rows and inserts the dataframe rows.
- `replace` builds and validates a new table before switching the live name.
- `truncate_insert` keeps the existing table shape, clears rows, then inserts.
- `upsert` stages rows and applies backend-specific replacement semantics.

The older `append` flag still works, but `write_mode` is clearer in shared code.
Do not mix both unless you are preserving compatibility with an existing call.

Empty dataframes use the same mode-aware contract as transfers. Replace and
truncate modes materialize an empty result by default; append and upsert keep
the target. Override this with `empty_source_policy="replace"`, `"keep"`, or
`"error"`. Empty object-typed columns require `table_schema` for replacement.

## Schema and Keys

By default, table creation uses dataframe columns and inferred backend types.
Pass `table_schema` when a column needs a specific backend-native type or when
source data can produce ambiguous pandas dtypes. Use
[sql.create_table](functions/create_table.md) when you need the table
creation step independently from loading rows.

Object columns whose non-null values are all Python `uuid.UUID` instances infer
as native `UUID` on Greenplum, Trino, and ClickHouse. ClickHouse uses
`Nullable(UUID)` when the dataframe column contains nulls. UUID-looking strings,
mixed object columns, and all-null object columns remain string types unless an
explicit `table_schema` selects `UUID`.

Inference inspects all dataframe values. Decimal columns use exact decimal types,
unsigned ClickHouse integer dtypes retain their unsigned family, and object
datetime columns retain timestamp precision and awareness. Trino inference uses
explicit `TIMESTAMP(6)` when every value aligns exactly; values needing more
precision use exact text unless the destination has an explicitly selected
supported native type. Native integer/decimal alternatives are tried before
text, and numeric values never pass through a floating-point intermediate.
Parquet staging uses the resolved physical schema for every column and file;
UUIDs and aware timestamps use the text representation required by Hive stages.

`key_columns` lets the load validate staged rows against an existing target
before final insertion. Use it when duplicate keys in append-like flows would be
more expensive to fix after the load finishes.

For `write_mode="upsert"`, `key_columns` is required. Incoming rows are staged
first and duplicate staged keys raise before finalization. If the target table
already exists, its schema is used for final insert column types. If the target
table does not exist, the load creates it from `table_schema` or inferred
dataframe types and inserts the dataframe rows normally.

Existing ClickHouse upserts bind target preservation reads, partition mutation,
final inserts and prepared-payload replay to the same verified physical table
and storage scope, including an exact local scope when appropriate. Caller
creation defaults continue to control new stages. Dry-run and returned plans stay offline and mark
`plan.options["pending_existing_layout_verification"] = True`: their provisional
`ALTER` scope reflects the requested policy until live execution verifies the
existing layout.

For Trino and ClickHouse upsert, `upsert_partition_column` is also required and
must name one dataframe column. That column defines the partition replacement
boundary; it is not inferred from `partition_by`, which only controls table
creation shape.

## Backend Notes

Greenplum dataframe inserts use chunked `execute_values` statements. Tune the
insert chunk size for very wide rows or constrained VMEM environments. Native
array columns retain their element precision and declared dimensions when
appending to an existing table. Nested UUIDs, exact decimals and nulls remain
native; array parameters use the resolved destination type. Quoted custom type
names retain their exact identity during table introspection.

Trino sends parameterized multi-row insert statements. A connection-level insert
chunk size can be set in `.connections`, and call-level settings can override
it. Native `ARRAY`, `MAP`, and `ROW` values retain nested UUID, Decimal and null
values. Named `ROW` fields follow the declared schema order, with missing or
extra fields rejected; positional rows must have the declared number of fields.
Map keys must remain unique and non-null under destination-native key identity after conversion.
UUID normalization, `REAL` rounding and `CHAR` padding can merge distinct source
keys; authoritative maps reject such collisions before target mutation instead
of discarding an entry. Map-key identity also uses the destination types inside
composite `ROW` keys,
so padding or rounding in a field cannot silently merge entries.

When Python serializes dataframe values to text, authoritative width checks use
the complete encoded strings, including structured-text metadata. Oversized
values fail before target creation or mutation.

Existing or explicit `TIMESTAMP(p)` destinations above precision six preserve
every available fractional digit when
the connector supports that precision. Inference retains its supported connector
precision. Trino upsert builds a final increment for affected partitions and requires
`upsert_partition_drop_sql_template` in `.connections` so connector-specific
partition drop SQL is explicit. Actual string partition values are quoted as
data, including marker-like values such as `<part>`. Only unknown partition
values in offline plans use a raw synthetic marker.

ClickHouse targets normally create and maintain a distributed/shard table pair.
Use `ch_only_shard=True` only when the target should intentionally be a local
ClickHouse table. ClickHouse upsert builds a final increment for affected
partitions, drops those partitions from the shard or local target, and inserts
through the target table.

ClickHouse timestamp columns without an explicit timezone use the actual
client/server write policy. The operation resolves and freezes that policy for
validation and retries without rewriting the column's SQL type. An explicit
column timezone takes precedence. Named zones use IANA `ZoneInfo` rules,
including future daylight-saving transitions; packaged `tzdata` and the
Python 3.8 backport provide portable support. Unknown timezone context is never
assumed to be UTC; dry-run plans mark required live resolution as pending.

Naive datetime and date values denote local wall time in the selected policy;
dates become midnight. For explicit or existing timestamp columns, ambiguous
or nonexistent local times fail before target mutation; supply an aware value
or exact instant instead. Inferred columns preserve unresolved local times and
mixed naive/aware values as exact text rather than selecting an offset or DST
fold. A server default alone does not disambiguate mixed awareness. Aware and
raw temporal values retain their physical instant and are never shifted twice.

Greenplum upsert deletes matching keys from the target using the stage table
and then inserts the staged rows.

## Progress and Results

Progress bars are opt-in. Enable them for longer loads where row-level feedback
helps, and keep them off in quiet automation.

Use metadata results when downstream code needs loaded row counts, retry
attempts, elapsed time, or the final target row count.

[SQL module index](index.md)
