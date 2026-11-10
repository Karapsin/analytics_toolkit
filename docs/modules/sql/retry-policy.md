[SQL module index](index.md)

# SQL Retry Policy

[sql.read](functions/read.md), [sql.execute](functions/execute.md),
[sql.load_df](functions/load_df.md), and [sql.transfer](functions/transfer.md)
stop immediately for the recognized errors below. Repeating unchanged SQL,
data, or connection configuration would not fix these failures. A stopped
operation may be submitted again after its cause has been addressed.

Unrecognized errors retain the historical retry fallback. A generic Python
`ValueError` or `TypeError` is not, by itself, proof that an operation must stop.
Native SQLSTATEs, error codes, and error names take precedence over message
matching. A permanent underlying failure remains permanent when a driver or
stage-load wrapper contains it, including in cross-backend transfers.

`retry_cnt`, `full_retry_cnt`, delays, and database-driver retry settings keep
their existing meanings. Drivers may retry individual HTTP requests within a
toolkit attempt; `retry_cnt=1` means one toolkit attempt, not necessarily one
network request. Retry layers do not share a new overall budget.

Generic transient write failures still retry with fresh connections, including
after a lost write or commit acknowledgement. Such replays may repeat effects
that already committed. The explicitly identified unknown-outcome codes below
stop instead. There is no automatic outcome reconciliation or polling, and
existing stage-finalization, recovery-artifact, cancellation, and ClickHouse
outage safeguards continue to apply. See [write safety](write-safety.md).

## Common Stop Conditions

- Invalid connection configuration — required fields or settings must change.
- Certificate-verification failure — correct the trust chain, certificate
  validity, or hostname before reconnecting.
- Permanent DNS name failure (`EAI_NONAME`) — correct the hostname or DNS record;
  temporary lookup failures remain eligible for retries.
- Missing required driver or optional native-driver dependency — install the
  dependency before rerunning the operation.
- Recognized deterministic input-validation failure — correct the input rather
  than repeating it.
- An existing explicit non-retryable or recovery-artifact safeguard — preserve
  its stop decision through wrappers and outer retry loops.

Cleanup still runs for stopped operations. Cleanup failures do not replace the
primary error, and closing a connection after established success does not
replay that successful operation. A replacement connection is opened only when
a further retry is eligible; terminal and exhausted failures never reconnect.

## ClickHouse

The following code/name pairs are recognized for HTTP and native connections.
Names follow the [ClickHouse error registry](https://github.com/ClickHouse/ClickHouse/blob/master/src/Common/ErrorCodes.cpp).

- `192 UNKNOWN_USER` — correct or create the configured account.
- `193 WRONG_PASSWORD` — the same password cannot authenticate.
- `194 REQUIRED_PASSWORD` — supply the missing password.
- `195 IP_ADDRESS_NOT_ALLOWED` — the client requires an access-policy change.
- `291 DATABASE_ACCESS_DENIED` — database permissions must change.
- `497 ACCESS_DENIED` — grant the required privilege.
- `516 AUTHENTICATION_FAILED` — correct credentials or authentication configuration.
- `36 BAD_ARGUMENTS` — correct invalid arguments.
- `43 ILLEGAL_TYPE_OF_ARGUMENT` — correct the argument types.
- `46 UNKNOWN_FUNCTION` — correct the function name or server compatibility.
- `47 UNKNOWN_IDENTIFIER` — correct the column, alias, or identifier.
- `48 NOT_IMPLEMENTED` — retries cannot add unsupported functionality.
- `50 UNKNOWN_TYPE` — correct the type name or server compatibility.
- `53 TYPE_MISMATCH` — add suitable casts or change incompatible types.
- `57 TABLE_ALREADY_EXISTS` — reconcile the existing table or creation logic.
- `58 TABLE_METADATA_ALREADY_EXISTS` — reconcile existing table metadata.
- `60 UNKNOWN_TABLE` — correct or create the referenced table.
- `62 SYNTAX_ERROR` — correct the SQL text.
- `63 UNKNOWN_AGGREGATE_FUNCTION` — correct the aggregate or server compatibility.
- `80 INCORRECT_QUERY` — correct the query structure.
- `81 UNKNOWN_DATABASE` — correct or create the referenced database.
- `82 DATABASE_ALREADY_EXISTS` — reconcile the existing database or creation logic.
- `115 UNKNOWN_SETTING` — correct or remove the unsupported setting.
- `184 ILLEGAL_AGGREGATION` — correct aggregation or grouping expressions.
- `386 NO_COMMON_TYPE` — add suitable casts or change incompatible expressions.
- `6 CANNOT_PARSE_TEXT` — correct the input text or format.
- `20 NUMBER_OF_COLUMNS_DOESNT_MATCH` — align input columns with the target.
- `70 CANNOT_CONVERT_TYPE` — correct the value or requested conversion.
- `117 INCORRECT_DATA` — the same malformed data will fail again.
- `248 INVALID_PARTITION_VALUE` — correct the partition value or expression.
- `321 VALUE_IS_OUT_OF_RANGE_OF_DATA_TYPE` — change the value or widen its type.
- `349 CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN` — supply a value or change nullability.
- `407 DECIMAL_OVERFLOW` — correct the value, precision, scale, or calculation.
- `164 READONLY` — use an authorized write-capable session or account.
- `217 CLIENT_HAS_CONNECTED_TO_WRONG_PORT` — correct the protocol/port pairing.
- `229 QUERY_IS_TOO_LARGE` — shorten the query or change its limit.
- `392 QUERY_IS_PROHIBITED` — change the query or governing policy.
- `394 QUERY_WAS_CANCELLED` — respect cancellation rather than recreating the workload.
- `452 SETTING_CONSTRAINT_VIOLATION` — choose a permitted setting value.
- `471 INVALID_SETTING_VALUE` — correct the setting value.

The following resource errors also stop by default. The toolkit does not infer
resource recovery, wait for a quota reset, or monitor merge progress before
resubmitting work:

- `241 MEMORY_LIMIT_EXCEEDED` — an unchanged query can exceed the same memory cap.
  Shared pressure can sometimes recover, but this code does not establish that
  retrying is appropriate.
- `159 TIMEOUT_EXCEEDED` — an unchanged query can exceed the same execution limit.
  For distributed DDL, inspect the existing task: it may continue in the background.
- `160 TOO_SLOW` — investigate the query or execution-speed threshold.
- `243 NOT_ENOUGH_SPACE` — restore storage capacity before rerunning the operation.
- `201 QUOTA_EXCEEDED` — wait for the applicable quota reset or change the quota.
- `252 TOO_MANY_PARTS` — establish backpressure and merge progress; repeated writes
  can worsen the backlog.

Fixed limits differ from temporary concurrency or transport failures. For
example, `202 TOO_MANY_SIMULTANEOUS_QUERIES`, `203 NO_FREE_CONNECTION`,
`209 SOCKET_TIMEOUT`, and `210 NETWORK_ERROR` remain eligible for retries,
subject to existing safeguards. See the server guidance on
[query limits](https://clickhouse.com/docs/operations/settings/query-complexity),
[quotas](https://clickhouse.com/docs/operations/quotas), and
[too many parts](https://clickhouse.com/docs/resources/support-center/knowledge-base/troubleshooting/exception-too-many-parts).

Explicit running/unknown-outcome errors stop automatic resubmission:

- `216 QUERY_WITH_SAME_ID_IS_ALREADY_RUNNING` — inspect or wait for the existing
  query instead of submitting another operation.
- `319 UNKNOWN_STATUS_OF_INSERT` — establish the insert outcome or deduplication
  safety before another submission; the error does not prove nothing was inserted.

Branded ClickHouse HTTP gateway `401` and `403` responses stop even when no
native server code is present: correct authentication or access policy first.
Other HTTP status codes retain the historical fallback. Typed certificate
verification failures in the native driver also stop; an unclassified generic
network exception remains eligible for retries.

Legacy ClickHouse wrapper aliases also retain their stop behavior when no
structured native code overrides them:

- `ALREADY_EXISTS` — reconcile the existing object or creation logic.
- `COLUMN_NOT_FOUND` — correct the column reference.
- `FEATURE_NOT_SUPPORTED` — retries cannot add unsupported functionality.
- `FUNCTION_NOT_FOUND` — correct the function name or signature.
- `INSUFFICIENT_PRIVILEGE` — change the required privileges.
- `SCHEMA_NOT_FOUND` — correct or create the schema.
- `TABLE_NOT_FOUND` — correct or create the table.

Leading ClickHouse server diagnostics reporting that a value cannot parse or
convert also stop when no structured native code is available. Correct the
input or conversion; the same text inside quoted SQL does not establish an
input failure.

## Trino

These names follow the [standard error registry](https://github.com/trinodb/trino/blob/master/core/trino-spi/src/main/java/io/trino/spi/StandardErrorCode.java).
Neither `USER_ERROR`, `INTERNAL_ERROR`, `INSUFFICIENT_RESOURCES`, nor `EXTERNAL`
alone makes every error terminal. The recognized name and its underlying cause
determine the stop decision.

- `SYNTAX_ERROR` — correct the SQL text.
- `CATALOG_NOT_FOUND` — correct or provision the catalog.
- `SCHEMA_NOT_FOUND` — correct or create the schema.
- `TABLE_NOT_FOUND` — correct or create the table.
- `COLUMN_NOT_FOUND` — correct the column reference.
- `FUNCTION_NOT_FOUND` — correct the function or signature.
- `ALREADY_EXISTS` — reconcile the existing object or creation logic.
- `TYPE_MISMATCH` — correct incompatible types.
- `INVALID_CAST_ARGUMENT` — correct the value or requested conversion.
- `INVALID_FUNCTION_ARGUMENT` — correct the function input.
- `DIVISION_BY_ZERO` — correct the calculation or data.
- `NUMERIC_VALUE_OUT_OF_RANGE` — correct the value or type.
- `PERMISSION_DENIED` — change the required privileges.
- `CONSTRAINT_VIOLATION` — correct the rejected data.
- `NOT_SUPPORTED` — retries cannot add unsupported functionality.
- `INVALID_SESSION_PROPERTY` — correct session configuration.
- `INVALID_TABLE_PROPERTY` — correct table configuration.
- `CONFIGURATION_INVALID` — repair server configuration.
- `COMPILER_ERROR` — investigate the query or engine.
- `QUERY_EXCEEDED_COMPILER_LIMIT` — reduce query complexity.
- `USER_CANCELED` — respect cancellation.
- `ADMINISTRATIVELY_KILLED` — respect administrative termination.

Legacy wrapper aliases also retain their stop behavior when exposed through
`name` or `error_name`:

- `FEATURE_NOT_SUPPORTED` — retries cannot add unsupported functionality.
- `INSUFFICIENT_PRIVILEGE` — change the required privileges.

Fixed query limits stop unchanged retries:

- `EXCEEDED_GLOBAL_MEMORY_LIMIT` — the query exceeds its memory cap across workers.
- `EXCEEDED_LOCAL_MEMORY_LIMIT` — the query exceeds its memory cap on one worker.
- `EXCEEDED_FUNCTION_MEMORY_LIMIT` — the function exceeds its memory allowance.
- `EXCEEDED_TIME_LIMIT` — the query exceeds its duration limit.
- `EXCEEDED_CPU_LIMIT` — the query exceeds its CPU budget.
- `EXCEEDED_SPILL_LIMIT` — the query exceeds its spill allowance.
- `EXCEEDED_SCAN_LIMIT` — the query exceeds its scan allowance.
- `EXCEEDED_WRITE_LIMIT` — the operation exceeds its write allowance.
- `EXCEEDED_OUTPUT_LIMIT` — the result exceeds its output allowance.

The global memory limit above applies to one query across workers. Temporary
shared pressure such as `QUERY_QUEUE_FULL` or `CLUSTER_OUT_OF_MEMORY` remains
eligible for retries. See [resource-limit semantics](https://trino.io/docs/current/admin/properties-resource-management.html).

Recognized connector failures stop even when their type is `EXTERNAL`:

- `HIVE_BAD_DATA` — repair source data.
- `HIVE_PARTITION_SCHEMA_MISMATCH` — reconcile table and partition schemas.
- `HIVE_INVALID_METADATA` — repair catalog metadata.
- `HIVE_UNSUPPORTED_FORMAT` — change the data format or connector capability.
- `ICEBERG_BAD_DATA` — repair source data.
- `ICEBERG_INVALID_METADATA` — repair Iceberg metadata.
- `ICEBERG_INVALID_PARTITION_VALUE` — correct partition values.

These names follow the [Hive](https://github.com/trinodb/trino/blob/master/plugin/trino-hive/src/main/java/io/trino/plugin/hive/HiveErrorCode.java)
and [Iceberg](https://github.com/trinodb/trino/blob/master/plugin/trino-iceberg/src/main/java/io/trino/plugin/iceberg/IcebergErrorCode.java)
error registries. Other connector or commit errors retain their existing retry
handling and ambiguity safeguards.

Authentication and endpoint failures stop after the driver's normal
authentication handling:

- Persistent HTTP `401` — correct credentials or authentication configuration.
- HTTP `403` — correct access policy or permissions.
- HTTP `404` identified as an invalid submission endpoint — correct the URL or
  routing. An arbitrary result-fetch `404` is not sufficient for this rule.
- `TrinoAuthError` — correct the authentication failure rather than restarting
  authentication indefinitely.

## Greenplum

SQLSTATE families `0A`, `21`, `22`, `23`, `28`, `3D`, `3F`, `42`, `44`, `54`, and
`F0` stop unchanged operations. They cover unsupported features, cardinality,
data, constraints, authentication, invalid database/schema, SQL/access errors,
view checks, hard program limits, and server configuration. This includes
previously unlisted codes within those known families; unrelated unknown codes
retain the historical fallback.

Libpq startup diagnostics for a rejected password, missing role, `pg_hba`
access rejection, missing database, or TLS verification failure are also
recognized when no SQLSTATE is available. Correct the corresponding credentials,
access rule, database, or certificate configuration before reconnecting. A
generic `OperationalError` alone retains the historical retry fallback.

The following named cases explain the common stop conditions. Names and codes
follow the [PostgreSQL error definitions](https://www.postgresql.org/docs/current/errcodes-appendix.html)
and [psycopg mapping](https://www.psycopg.org/docs/sqlstate_errors.html). A given
Greenplum version may not emit every cataloged code.

- `0A000 FeatureNotSupported` — retries cannot add unsupported functionality.
- `21000 CardinalityViolation` — correct unexpected row counts or query logic.
- `22001 StringDataRightTruncation` — shorten the value or widen the destination.
- `22003 NumericValueOutOfRange` — correct the value or numeric type.
- `22007 InvalidDatetimeFormat` — correct the date/time representation.
- `22012 DivisionByZero` — correct the expression or data.
- `22P02 InvalidTextRepresentation` — correct input that cannot parse as its type.
- `23502 NotNullViolation` — supply required values.
- `23505 UniqueViolation` — resolve duplicate keys; unchanged input repeats the conflict.
- `23514 CheckViolation` — correct data violating the constraint.
- `28000 InvalidAuthorizationSpecification` — correct authentication/access configuration.
- `28P01 InvalidPassword` — correct the credentials.
- `3D000 InvalidCatalogName` — correct or provision the database.
- `3F000 InvalidSchemaName` — correct or provision the schema.
- `42501 InsufficientPrivilege` — change the required permissions.
- `42601 SyntaxError` — correct the SQL text.
- `42702 AmbiguousColumn` — qualify the column reference.
- `42703 UndefinedColumn` — correct the column name or schema.
- `42704 UndefinedObject` — correct or provision the referenced object.
- `42803 GroupingError` — correct aggregation/GROUP BY logic.
- `42804 DatatypeMismatch` — correct incompatible types.
- `42883 UndefinedFunction` — correct the function, signature, or installation.
- `42P01 UndefinedTable` — correct or provision the table.
- `42P07 DuplicateTable` — reconcile the existing table or creation logic.
- `44000 WithCheckOptionViolation` — correct the write violating the view condition.
- `54001 StatementTooComplex` — simplify the statement.
- `54011 TooManyColumns` — reduce the column count.
- `54023 TooManyArguments` — reduce the argument count.
- `57014 QueryCanceled` — respect cancellation; a statement timeout requires an
  explicit recovery decision rather than an unchanged automatic retry.
- `F0000 ConfigFileError` — repair server configuration.
- `F0001 LockFileExists` — investigate the server startup/lock condition.
- `XX001 DataCorrupted` — investigate and repair the data.
- `XX002 IndexCorrupted` — investigate and repair the index.

Resource failures also stop by default:

- `53100 DiskFull` — restore storage capacity before rerunning the operation.
- `53200 OutOfMemory` — investigate query demand or shared memory pressure.
- `53400 ConfigurationLimitExceeded` — correct an exceeded ceiling or its cause.
  Greenplum also uses this code for some shared/OS memory pressure; the code
  alone does not establish that retrying is appropriate.

The toolkit does not automatically detect pressure recovery or enable a
resource retry override. See [Greenplum memory-error guidance](https://knowledge.broadcom.com/external/article/296174/how-to-identify-out-of-memory-oom-errors.html).

Explicit unknown-outcome codes stop automatic replay:

- `08007 TransactionResolutionUnknown` — establish the transaction outcome before
  another submission.
- `40003 StatementCompletionUnknown` — establish statement completion before
  another submission.

`40001 SerializationFailure` and `40P01 DeadlockDetected` retain complete-operation
retries and rollback behavior. A `23505` caused by an application-level
serialization race can also justify restarting the complete transaction, but
that requires application knowledge and remains outside generic duplicate-key
retry handling. See [transaction retry guidance](https://www.postgresql.org/docs/current/mvcc-serialization-failure-handling.html).

[SQL module index](index.md)
