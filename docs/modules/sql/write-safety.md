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
[sql.create_sql_table](functions/create_sql_table.md), and
[sql.drop_many_partitions](functions/drop_many_partitions.md). ClickHouse table
plans can also come from [sql.ch_drop_table](functions/ch_drop_table.md).

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

[SQL module index](index.md)
