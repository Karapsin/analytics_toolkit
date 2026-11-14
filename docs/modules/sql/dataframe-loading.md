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

Dataframe write column names must be unique as SQL strings. When a write keeps
an existing target schema, every incoming column must be present and insertable;
unknown names and noninsertable fields fail before stage creation or target mutation.
Backend-specific supported inputs, such as ClickHouse `EPHEMERAL` columns, remain
available. Valid subsets and
reordered columns remain supported for `append` and `truncate_insert`.
Replacement follows its requested or inferred schema and can include new columns.

For nonempty input, `append`, `truncate_insert` and `upsert` validate converted
SQL nulls and required columns before mutating an existing target. A null for a
`NOT NULL` column or an omitted required column fails early. Append and truncate
loads retain valid nullable omissions and Greenplum default, generated and identity
handling; upserts still require complete writable rows. Empty inputs retain
`empty_source_policy` behavior.

Python `Enum` members use their logical `.value` before type resolution,
validation and transport, including nested values. Their underlying values follow
the usual native conversions and null handling; a string-valued member follows
the same conversion as its string value.

An explicit or existing integer column converts accepted boolean scalars to
native `0` and `1`. Automatic inference keeps boolean input boolean and integer
`0`/`1` input integer.

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
Key-validated append uses explicit incoming column names in both `INSERT` and
`SELECT`, including dry-run and returned SQL plans, so valid subsets and reordered
inputs keep the same mapping as execution.

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

Supported Greenplum built-ins follow exact quoted identity, including recognized
`pg_catalog`-qualified names, while rendering the supplied schema SQL.
`NUMERIC(p)` has scale zero, SQL `CHAR` defaults to length one, and bare `BPCHAR`
has unbounded input validation. Redundant fractional zeros can be removed to fit
a numeric scale exactly; nonzero excess fractional digits are rejected.

Greenplum `TIME` and `INTERVAL` accept native `datetime.time` and
`datetime.timedelta` values, including microsecond-aligned pandas timedeltas.
Existing and inferred native schemas require exact precision. An explicit schema
can request PostgreSQL rounding at precision 0–6 and native interval field
restrictions. Rounding retains the `24:00` boundary and separate interval day/time
components. Finer nanoseconds require explicit rounding or exact typed text
through inference. Mixed or unresolved clock awareness also uses exact typed text
during inference.
Use SQL `INTERVAL(p)` for precision; quoted or catalog-qualified interval typmod
argument forms are rejected.

Declared `TIME` keeps wall-clock fields and discards a supplied zone. `TIMETZ`
(`TIME WITH TIME ZONE`)
without an offset uses the server's current date and session timezone; resolved
offsets require whole seconds and magnitude below 16 hours. A timedelta carries
day-time components and cannot recover the original calendar-month identity of
a decoded SQL interval.

Greenplum `JSON` and `JSONB`, including their `pg_catalog`-qualified forms,
accept dictionaries, lists, encoded JSON document strings and supported psycopg2
`Json` wrappers. Strings retain their document text. Serialization and validation
finish before target mutation; retained documents are reused on retries, so a
custom serializer runs once. Malformed documents and values outside the `JSONB`
Unicode or numeric domain fail early. Python `None` is SQL NULL; the document
`'null'` and `Json(None)` are JSON null and can satisfy a `NOT NULL` column.
Explicit JSON previews may serialize and validate once in memory without opening
a connection.

Declare native SQL array dimensions explicitly, such as `JSONB[][]` for a
two-dimensional array. Prepared values retain that rank when catalog metadata
reports `JSONB[]`. The default Greenplum query decoder maps SQL NULL and JSON
null to `None`; decoded lists and catalog `[]` metadata can also leave the
original SQL array rank ambiguous.

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

Trino `TIME` map keys use destination wall-clock identity and canonical key
payloads without offsets. `TIME WITH TIME ZONE` keys retain known offsets and
compare by UTC time modulo 24 hours; naive zoned keys fail before mutation
because their identity requires an unresolved offset. These rules also apply
inside composite `ROW` keys. Scalar clock values retain native server cast policy.

Trino timestamp map keys use the destination precision and native parameter
conversion to establish identity. Equivalent instants encoded at different
source tick scales reject before mutation; distinct supported fractional ticks
and DST-fold instants retain separate entries. Zoned keys require an aware
instant or a resolved destination timezone policy. These checks also recurse
through composite keys.

Existing or explicit Trino `TIME` schemas accept native time values. Bare `TIME`
defaults to precision 3; the engine range is 0–12, subject to connector support.
Existing precision must fit exactly; explicit conversion uses native rounding
with midnight wrapping to `00:00`. Zoned offsets require whole minutes within
±14 hours. Unresolved named zones fail before mutation rather than inventing a
client date; matching and cross-zone casts use server policy. Nonempty native
`TIME` payloads require native insertion instead of incompatible Hive/Parquet
staging. Omitting a nullable target `TIME` column remains valid, and empty-input
policies retain native SQL handling.

When Python serializes dataframe values to text, authoritative width checks use
the complete encoded strings, including structured-text metadata. Oversized
values fail before target creation or mutation.

Existing or explicit `TIMESTAMP(p)` destinations above precision six preserve
every available fractional digit when the connector supports that precision,
including exact temporal cells returned by [reads](functions/read.md#notes).
Trino destinations may support precision 9 or 12; default inference remains
precision 6 and never upgrades the table format. Greenplum microsecond and
ClickHouse precision-9 limits remain unchanged. Existing schemas require exact
fit; an explicit lower precision remains a deliberate conversion.
Trino upsert builds a final increment for affected partitions and requires
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
