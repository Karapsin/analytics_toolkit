[SQL module index](index.md)

# Write Safety

Write-heavy SQL workflows should be inspectable before they mutate a database.
The SQL module supports dry-run plans, metadata result wrappers, query labels,
and readable plan formatting for that purpose.

## Dry Runs and Plans

Use dry runs when reviewing generated SQL for table creation, loads, transfers,
partition changes, and backend-specific table operations. A dry-run plan
contains ordered statements, connection aliases, backend names, targets, and
notable options.

Common planned workflows include [sql.load_df](functions/load_df.md),
[sql.transfer](functions/transfer.md),
[sql.create_table](functions/create_table.md),
[sql.execute_create](functions/execute_create.md), and
[sql.drop_partitions](functions/drop_partitions.md). Query-result writes from
[sql.insert](functions/insert.md) and
[sql.execute_insert](functions/execute_insert.md) are also plan-aware.
ClickHouse table plans can also come from
[sql.drop_tables](functions/drop_tables.md).

Operations that require live inspection for exact SQL may include placeholder
plan steps instead of opening a connection. Treat those placeholders as a
signal that runtime metadata is needed before the final SQL can be known.

## Metadata Results

Metadata results keep historical return values available while adding elapsed
seconds, retry attempts, statement counts, operation status, row counts where
available, and labels. Use them when automation needs to assert what happened
after a write.

Use [sql.format_plan](functions/format_plan.md) when a dry-run plan needs to be
rendered as readable text.

## Labels

Query labels are safe SQL comments attached to generated statements, logs, and
metadata. Use stable labels for scheduled jobs so database activity, Python
logs, and operation metadata can be connected later.

## Atomicity and Retry Boundaries

Validation failures before mutation leave an existing target unchanged. Staged
append and upsert workflows do not mutate the target until finalization, and a
target created only for a failed operation is cleaned up; cleanup never removes
a pre-existing target.

| Backend/finalization | Failure contract |
| --- | --- |
| Greenplum transactional finalization | rolls back to the exact original target |
| Trino/Iceberg partition replacement | target is either exactly original or exactly committed; partial/mixed state is never reported as success |
| ClickHouse partition replacement | target is either exactly original or exactly committed; partial/mixed state is never reported as success |
| destructive replace/truncate on a non-transactional backend | no preservation promise after the destructive statement; failure remains contextual and stages are cleaned |

Staged load and transfer retry-safe phases reopen a connection and start from a
clean attempt; their existing finalization and ambiguity safeguards still apply.
`execute`, `insert`, `execute_insert`, and `execute_create` retry the whole
operation on transient failures with fresh connections, including after mutation
submission or a lost commit acknowledgement. Replays can repeat already committed
effects. `retry_cnt` bounds attempts, `timeout_increment` controls backoff, and
`retry_cnt=1` requests one attempt. Deterministic SQL errors are not retried.
These retries provide at-least-once delivery across all three backends, including
ClickHouse native writes. If an earlier Trino chunk committed before a later
chunk failed, restarting the operation can repeat it. Acknowledgement loss can
also repeat a fully accepted write. Optional metadata reports whether replay was
possible; it does not certify exactly-once delivery.

Connection and cursor close failures are logged and cannot replay successful
work or replace the primary operation exception. Semantic finalization failures
remain errors. Toolkit-managed uncertain structural cutovers retain recovery
artifacts and are not blindly replayed.

[SQL module index](index.md)
