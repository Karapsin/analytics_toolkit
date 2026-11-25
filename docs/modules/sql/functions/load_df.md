[SQL functions index](index.md)

# load_df

Load a pandas dataframe into a SQL table on a configured backend.

```python
load_df(db_key: 'str', destination_table: 'str', df: 'pd.DataFrame', append: 'bool' = False, write_mode: 'str | None' = None, gp_distributed_by_key: 'str | Sequence[str] | None' = None, gp_partitions: 'Mapping[str, Any] | None' = None, key_columns: 'str | Sequence[str] | None' = None, upsert_partition_column: 'str | None' = None, retry_cnt: 'int' = 5, timeout_increment: 'float' = 5, trino_insert_chunk_size: 'int | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str | None' = None, ch_cluster: 'str | None' = None, ch_sharding_key: 'str | None' = None, ch_distributed_table: 'bool | None' = None, ch_distributed_engine_template: 'str | None' = None, ch_distributed_cluster: 'str | None' = None, ch_shard_on_cluster: 'str | None' = None, ch_distributed_on_cluster: 'str | None' = None, ch_ddl_ready_timeout_seconds: 'float | None' = None, ch_ddl_wait_policy: 'str | None' = None, ch_only_shard: 'bool' = False, ch_retry_per_host_drops: 'bool' = True, dry_run: 'bool' = False, return_sql: 'bool' = False, return_metadata: 'bool' = False, query_label: 'str | None' = None, gp_insert_chunk_size: 'int | None' = None, progress: 'bool' = False, table_schema: 'dict[str, str] | None' = None, empty_source_policy: 'EmptySourcePolicy | None' = None, *, ch_unavailable_shards: 'str | None' = None) -> 'int | SqlPlan | SqlOperationResult'
```

## Inputs

### General Inputs

- `db_key` - connection key or alias from `.connections`; backend dispatch is selected from that entry
- `destination_table` - target table name for dataframe loading
- `df` - dataframe to load
- `append` - historical dataframe loading flag; `True` appends and `False` replaces unless `write_mode` is supplied
- `write_mode` - explicit write behavior: append, replace, truncate_insert, or upsert
- `empty_source_policy` - zero-row behavior: `replace`, `keep`, or `error`; omission resolves to `replace` for replace/truncate modes and `keep` for append/upsert
- `key_columns` - key column or columns used to validate staged rows and required when `write_mode="upsert"`; upserts prepend the partition column when supplied
- `upsert_partition_column` - staged column added to the effective upsert key on every backend; also defines affected partitions and is required for Trino and ClickHouse upserts
- `retry_cnt` - number of operation retries with fresh connections
- `timeout_increment` - delay increment used between operation retries
- `dry_run` - when `True`, return a plan without mutating the database
- `return_sql` - when `True`, return a `SqlPlan` instead of mutating a database
- `return_metadata` - when `True`, return `SqlOperationResult` instead of the historical bare value
- `query_label` - safe label added to generated SQL comments, plans, metadata, and logs
- `progress` - whether to show progress bars for supported multi-step or row-loading operations
- `table_schema` - explicit backend-native column type mapping for created tables
- `partition_by` - partitioning columns or expression for created tables, interpreted according to the target backend
- `order_by` - ordering or sorting columns or expression for created tables, interpreted according to the target backend

### Backend-Specific Inputs

- `ch_unavailable_shards` - ClickHouse outage policy: `None` uses the connection default (`stop` when omitted); `healthy_shards` enables ad hoc partial work with prominent warnings; see [outage policies](../clickhouse-distributed-tables.md#unavailable-shards)

- `gp_distributed_by_key` - distribution key column or columns for created Greenplum target tables
- `gp_partitions` - initial Greenplum range or list child definitions used when the final target is created or replaced
- `gp_insert_chunk_size` - dataframe insert page size for Greenplum
- `trino_insert_chunk_size` - number of rows per Trino parameterized multi-row insert statement
- `ch_engine` - engine to use for created ClickHouse local shard tables
- `ch_cluster` - deprecated compatibility shortcut for execution and routing clusters
- `ch_sharding_key` - sharding expression for ClickHouse distributed table creation
- `ch_distributed_table` - override for configured pair or single-table topology
- `ch_distributed_engine_template` - override for the configured Distributed engine template
- `ch_distributed_cluster` - routing cluster inside the Distributed engine
- `ch_shard_on_cluster` - execution cluster for shard DDL
- `ch_distributed_on_cluster` - execution cluster for Distributed facade DDL
- `ch_ddl_ready_timeout_seconds` - overall ClickHouse post-create readiness deadline; explicit values override the connection default
- `ch_ddl_wait_policy` - `wait_all`, `wait_shard`, `wait_distr`, or `wait_none`; explicit values override the ClickHouse connection policy
- `ch_only_shard` - for ClickHouse, create or mutate only the local table instead of a distributed/shard pair
- `ch_retry_per_host_drops` - whether ClickHouse replace/drop flows may retry direct local drops on affected hosts

## Usage

```python
import pandas as pd
from analytics_toolkit import sql

scores = pd.DataFrame({"user_id": [1, 2], "score": [10.5, 12.0]})
rows = sql.load_df(
    db_key="gp",
    destination_table="sandbox.scores",
    df=scores,
    write_mode="truncate_insert",
)
```

Output example:

```python
rows
# 2
```

## Notes

- Recognized permanent errors stop stage and operation retries, including when
  an insert failure is wrapped by the driver or loading flow. Unrecognized
  errors retain the existing retry fallback. See the [retry policy](../retry-policy.md).
- Every SQL data-stage name created by `load_df` begins with the first 16 hex
  characters of SHA-256 over the exact canonical destination, followed by
  `__`. This stable prefix is collision-resistant but is not proof of identity.
  Existing names are never reused or overwritten; runtime collisions allocate
  another name, and cleanup retains the actual created name.
- Omitting `write_mode` selects `replace` unless the legacy `append=True` flag is supplied. Prefer an explicit `write_mode` for incremental or truncate-and-load workflows.
- Replacing an existing table builds and validates an offside table before cutover. Greenplum uses a transaction, Trino uses a reversible rename, and ClickHouse exchanges or reversibly renames the physical/facade replacement pair. New columns are included without dropping the live table first.
- Dataframe write column names must be unique as SQL strings. For modes that
  retain an existing target schema, unknown or noninsertable incoming fields fail
  before stage creation or target mutation. Backend-supported input columns remain
  available. Valid subset and reordered inputs remain supported
  for `append` and `truncate_insert`; replacement uses its new resolved schema.
  Key-validated append plans name incoming columns explicitly in both `INSERT`
  and `SELECT`, matching execution.
- Nonempty `append`, `truncate_insert` and `upsert` inputs reject converted SQL
  nulls for `NOT NULL` columns and omitted required columns before target mutation.
  Backend-supported append/truncate omissions, complete-row upsert requirements
  and empty-input behavior are preserved.
- Python `Enum` members resolve through `.value`, recursively, before validation
  and native or text conversion. Underlying strings, exact numbers and nulls keep
  their normal conversion rules.
- Explicit or existing numeric schemas accept supported numeric text through
  backend-owned conversion before destination validation. Greenplum integer text
  follows native ASCII syntax and the signed 16/32/64-bit destination bounds.
  Direct Trino `BIGINT` uses Python `int(value)` semantics within BIGINT bounds,
  including truncation of finite numeric inputs; other Trino integer text keeps
  native syntax. ClickHouse `Int`/`UInt` widths from 8 through 256 bits preserve
  Python integer conversions and enforce the declared signed/unsigned bounds.
  String inference remains text.
- Supported finite floating-point and Decimal text is checked against the
  destination type. Floating conversion follows its native width; Greenplum
  parameter adaptation preserves signed zero. Decimal precision and scale are
  checked exactly without a floating-point detour. Nonfinite text tokens reject
  before mutation rather than becoming SQL NULL. These numeric conversions apply
  only to declared numeric scalar leaves; unrelated temporal fields retain their
  identity and timezone handling.
- Explicit or existing integer columns convert accepted boolean scalars to native
  `0`/`1`; automatic boolean and integer inference retain their respective types.
- Trino binary columns preserve `bytes`, `bytearray` and `memoryview` payloads,
  including nested native containers. SQL NULL remains NULL.
- Existing or explicit Trino `IPADDRESS` accepts IPv4/IPv6 text and 4- or 16-byte
  packed addresses. Numeric IPv6 scopes from 0 to 2147483647 are accepted. Named
  interface scopes reject before mutation; use unscoped text or packed bytes.
- Existing or explicit ClickHouse `IPv4`/`IPv6` accepts address text, Python
  address objects, packed bytes and unsigned integers in the declared family's
  range. IPv6 integer `1` means `::1`; explicit IPv4 text, objects or packed bytes
  promoted to IPv6 become IPv4-mapped addresses. Both transports use the same
  native address values. Scoped addresses reject before mutation, and SQL NULL
  requires a `Nullable` type. These rules apply inside native containers too.
- ClickHouse keeps supported aliases in the supplied schema SQL. Python writes
  also require support from the selected client's codecs for the actual writer
  column types. Required value checks cover every converted batch before live
  mutation. Certified retries reject changed connection or client settings before
  another write. Metadata-only type-domain proofs report no scanned-row count.
  Empty operations finalized entirely in SQL do not instantiate row codecs;
  offline plans leave unresolved client validation pending.
- ClickHouse HTTP `String` writes encode text as UTF-8 bytes and preserve binary
  values and SQL NULL, recursively inside native containers. Native writes
  retain the configured string encoding.
- Supported Greenplum built-ins retain qualified/quoted identity and the supplied
  schema SQL. `NUMERIC(p)` uses scale zero, SQL `CHAR` length one, and bare `BPCHAR`
  unbounded input validation. Excess fractional zeros can align exactly; nonzero
  excess fractions reject before mutation.
- Other authoritative Greenplum native types, including `CIDR` and domains, are
  validated by inserting into owned persistent staging tables before changing the
  live destination. Explicit and existing target schemas both retain authority;
  final insertion and retries reuse the certified native rows without adapting
  the Python values again. A retry cannot publish through a changed connection
  alias or checkpoint identity. This path requires staging-schema `CREATE` and type
  `USAGE` permissions. Offline plans set
  `plan.options["native_assignment_validation_pending"] = True` and create no stages.
- Existing or explicit Greenplum `INET` keeps valid address strings unchanged on
  the wire, including array elements. Native host bits and masks are preserved;
  key validation compares address family, address and prefix length. Invalid
  values reject before mutation. For INET-bearing composite keys, mixed
  naive/aware `TIMETZ` key values require explicit offsets for every clock value.
- Greenplum `TIME`/`INTERVAL` preserve supported native time/timedelta values.
  Existing and inferred precision must fit exactly; explicit precision requests
  use native rounding and interval field restrictions. See the
  [backend notes](../dataframe-loading.md#backend-notes) for `TIME`/`TIMETZ`
  zone handling and the calendar-month limitation of decoded timedeltas.
- Existing or explicit Trino `TIME` uses native binding with strict existing
  precision or requested native rounding; midnight wraps to `00:00`. Offsets use
  whole minutes within ±14 hours, and unresolved named zones fail before mutation.
  Nonempty native `TIME` payloads require native insertion; valid omitted nullable
  columns and empty-input policies retain their existing behavior.
- Trino map-key clocks use destination SQL identity, recursively inside `ROW`
  keys. `TIME` keys use wall-clock payloads without offsets; aware
  `TIME WITH TIME ZONE` keys retain offsets and compare modulo a UTC day.
  Naive zoned map keys reject before mutation. Scalar cast policy is unchanged.
- Trino timestamp map keys use destination precision and native parameter
  conversion for identity, including composite keys. Equivalent instants with
  different source tick scales reject before mutation; supported sub-microsecond
  differences remain distinct. Zoned keys require awareness or a resolved
  destination timezone policy.
- Trino native key equality composes across every declared `ROW` field and
  `ARRAY` element, including IPv4-mapped `IPADDRESS` aliases, `CHAR` padding,
  `REAL` rounding and temporal rules. For `ROW(address IPADDRESS, label CHAR(2))`,
  `('192.0.2.1', 'a')` and `('::ffff:192.0.2.1', 'a ')` are the same key;
  an authoritative map containing both rejects before target mutation.
  Key-validated append and upsert compare the complete effective key using the
  corresponding destination types, including any partition column. The existing
  rejection of SQL NULL in effective keys is unchanged.
- Trino `JSON` inserts use native `CAST` rules: strings become JSON strings,
  including document-looking text and `'null'`; `None` remains SQL NULL.
  To interpret document text, use `json_parse` explicitly in SQL.
- Greenplum `JSON`/`JSONB` and their `pg_catalog`-qualified forms preserve native
  dictionaries/lists, encoded document strings and supported `Json` wrappers.
  The validated serialized documents are retained through retries; custom
  serializers run once. Invalid JSON or stricter existing `JSONB` constraints
  reject before mutation. Python `None` remains SQL NULL, while `'null'` and
  `Json(None)` are JSON null. Explicit JSON previews validate in memory without
  opening a connection.
- Explicit native JSON array dimensions, such as `JSONB[][]`, survive catalog
  metadata reporting `JSONB[]`. See the [backend notes](../dataframe-loading.md#backend-notes)
  for the default query decoder's JSON-null and array-rank limitations.
- An empty dataframe follows `empty_source_policy`. Empty replacement creates a zero-row table with the requested schema. Pass `table_schema` when an empty object-typed column has no unambiguous backend type.
- `write_mode="upsert"` stages incoming rows and rejects duplicate staged keys
  before any target mutation.
- Existing-target upserts require every writable column, including columns with
  defaults. Generated and noninsertable columns are excluded. Missing or extra
  columns fail before stage creation or target mutation; reordered complete rows
  are loaded in target order. Explicit `table_schema` and existing target types
  remain authoritative and are validated against the complete dataframe.
  When Python serializes values to text, their complete encoded strings must
  fit the authoritative widths before target creation or mutation.
- Exact temporal cells returned by [Trino reads](read.md#notes) retain their ticks
  when an existing or explicit destination supports that precision. Default
  Trino inference remains precision 6; no table-format upgrade is performed.
  Greenplum microsecond and ClickHouse precision-9 limits remain unchanged.
- Existing ClickHouse upserts bind target preservation reads, partition mutation,
  final inserts and prepared-payload replay to the same verified physical storage
  scope; caller creation policy still controls new stages. Offline
  `dry_run`/`return_sql` plans open no connection and set
  `plan.options["pending_existing_layout_verification"] = True`. Their provisional
  partition SQL uses the requested policy until the existing layout is verified.
- ClickHouse timestamps use the [resolved write timezone](../dataframe-loading.md#backend-notes)
  without changing the declared column type. Explicit column zones win.
  Authoritative schemas reject ambiguous or nonexistent naive local times
  before mutation; inference preserves unresolved local times and mixed
  awareness as exact text. Aware and raw values retain their instant.
  Explicit and existing ClickHouse timestamp columns also accept native integer
  values: Unix seconds for `DateTime`, `DateTime32` and unscaled `TIMESTAMP`,
  and ticks of `10**(-scale)` seconds for `DateTime64(scale)`.
  A `TIMESTAMP(scale[, timezone])` with nonzero scale follows `DateTime64(scale)`.
  These values retain their units and are checked against
  the declared type before target mutation. Ordinary integer inference is
  unchanged.
  `DateTime64` integers must fit both the supported calendar interval at that
  scale and the signed tick representation; fitting the wire format alone is
  insufficient. The calendar limits match the existing timestamp-object checks
  and [ClickHouse's supported DateTime64 contract](https://github.com/ClickHouse/ClickHouse/blob/v26.3.15.4-lts/docs/en/sql-reference/data-types/datetime64.md).
- Trino `DATE` columns accept native dates, canonical `YYYY-MM-DD` text and
  supported datetime values under explicit or existing schemas. Datetimes use
  their supplied local calendar date, matching native parameter conversion.
  Every selected value is validated before destination mutation; NULL stays
  NULL, and inferred text columns retain their text types.
- Greenplum `DATE` columns also validate canonical date text before mutation.
  Native datetime parameters retain their clocks and offsets so the server
  performs the DATE assignment using its session settings.
- Explicit or existing built-in Greenplum `BOOL`/`BOOLEAN` columns accept native
  and NumPy Boolean scalars, NULL, and text using ASCII case-insensitive unique
  prefixes of `true`, `false`, `yes`, `no`, `on` or `off`, or exact `1`/`0`.
  Only surrounding ASCII whitespace (space, `\t`, `\n`, `\r`, `\v`, `\f`) is
  trimmed. Ambiguous `o`, empty text and invalid values reject before mutation
  with sanitized diagnostics. Domains retain server-assignment validation;
  numeric casts and automatic inference keep their existing rules.
- Explicit or existing Trino `BOOLEAN` columns accept native Boolean values,
  signed BIGINT, DOUBLE and finite DECIMAL casts, and the exact ASCII strings
  `true`, `false`, `t`, `f`, `1` and `0`, with ASCII case-insensitive letters.
  Numeric zero becomes false and nonzero becomes true, including infinite
  DOUBLE values; floating NaN retains its existing NULL handling. Decimals must
  fit precision and scale at most 38 and never pass through floating point.
  Strings are not trimmed.
  Invalid values fail before destination mutation with sanitized diagnostics;
  NULL stays NULL. Automatic inference is unchanged:
  inferred integer 0/1 columns remain integers, and inferred strings remain text.
- ClickHouse `Bool`/`Boolean` columns, including nullable wrappers, accept Boolean,
  integral, standard floating-point and Decimal scalars using exact zero/nonzero
  conversion. Decimals never pass through floating point, and the conversion
  imposes no integer or Decimal storage-width bound on values becoming Boolean.
  Existing NULL handling is unchanged. Strings and other truthy objects reject
  before mutation. Ordinary integer 0/1 inference remains integer.
- An explicit GP `TEXT`/`VARCHAR` or Trino `VARCHAR` stage feeding an existing
  built-in Boolean column validates every selected row against that backend's
  Boolean cast rules before destination mutation. Validation uses a converted
  view; the requested stage schema and raw string payload remain unchanged.
- Explicit GP `TEXT`/`VARCHAR`, Trino `VARCHAR` and ClickHouse `String` numeric
  stages also preserve their raw payloads and schemas. Final cast validation
  follows the destination's native SQL parser, which can differ from direct
  Python conversion, including direct Trino `BIGINT` conversion.
- ClickHouse timestamp keys compare native ticks at the declared precision and
  resolved write timezone. A raw tick value and a calendar value for the same
  instant are duplicate effective keys within one partition. Distinct native
  partitions retain partition-scoped key semantics.
- Staged append and upsert use the verified destination types for incoming
  columns when no explicit `table_schema` is supplied. Stage creation and client
  insertion use the same schema; reordered and subset append columns retain
  their matching types. Explicit stage schemas retain precedence, and
  intentional replacement keeps its schema-inference behavior.
- Key-validated Greenplum append checks uniqueness of stored stage keys. When an
  explicit incoming stage type differs from its corresponding actual target
  type, a final native owned checkpoint validates the converted keys before
  publication. Same-type and equivalent integer-alias schemas retain their fast
  path. Offline plans set `native_assignment_validation_pending=True` without
  database I/O.
- Explicit or existing ClickHouse `Array` schemas accept tuple sequences without
  changing the caller's values. Default inference still treats tuples as tuples.
- When `upsert_partition_column` is supplied, the effective key is that column
  followed by `key_columns`, with duplicates removed. ID 7 in January and ID 7
  in February are separate records. Without a partition column, `key_columns`
  retains its usual meaning. Ordinary append key validation is unchanged.
- When `load_df` creates a target table that did not exist at the start and the
  load later fails, it drops that newly created target during cleanup.
- Upsert requires `key_columns`. Greenplum uses staged key delete-and-insert and
  does not require `upsert_partition_column`; when supplied, it augments the
  transactional delete-and-insert key.
- Trino and ClickHouse upsert require `upsert_partition_column`. They build a
  final increment for affected partitions, drop those partitions, and insert
  the final increment. Arbitrary key deletes and merge-style updates are avoided
  because they are slow, connector-dependent, or mutation-heavy. The complete
  preserved/incoming payload is certified before partition deletion and reused
  across transient retries; preservation is never recomputed from a partially
  replaced target. An exhausted retry retains the recovery stage names.
- Trino partition upsert also requires `upsert_partition_drop_sql_template` in
  the target connection config so connector-specific partition drop SQL is
  explicit. Actual string partition values are quoted as data, including values
  beginning with `<`; a raw synthetic marker is reserved for unknown partition
  values in offline plans.
- For Trino connections with the complete `s3_transfer_staging_schema` and
  `s3_transfer_staging_location` pair, `load_df` writes temporary Parquet files
  to the object-storage prefix, creates the external stage table in the S3
  staging schema, and finalizes into the target from that table. Python and
  Trino must both be able to access and clean up the prefix.
- If the S3 pair is not configured, Trino `load_df` is not Parquet-based and
  uses direct dataframe inserts controlled by `trino_insert_chunk_size`.
  `transfer_staging_schema` remains available for ordinary SQL staging.
- ClickHouse target creation follows the resolved connection creation policy;
  existing append/upsert targets retain their verified physical layout. Stage
  cleanup drops only resources acknowledged as created by the current run. A
  visible collision or uncertain CREATE result does not establish ownership.
- SQL stage identifiers use one maximum-63-byte naming policy on Greenplum,
  Trino, and ClickHouse. Backend-specific catalog/schema qualification and
  quoting are preserved.
- Greenplum validates `gp_partitions` on every call, but renders it only when
  creating or recreating the final target. Existing append, truncate, and
  upsert targets are not repartitioned implicitly.

[SQL functions index](index.md)
