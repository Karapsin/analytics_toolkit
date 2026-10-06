[SQL module index](index.md)

# Logging and Observability

SQL workflows emit timing and status information so database work can be traced
without printing every query by default.

## Query Visibility

Query text is hidden by default. Enable query printing only when reviewing SQL
locally or debugging a specific task. For scheduled jobs, prefer labels and
structured logs over full query text.

Query printing and labels are available on common entrypoints such as
[sql.read](functions/read.md), [sql.execute](functions/execute.md),
[sql.execute_read](functions/execute_read.md),
[sql.load_df](functions/load_df.md), and [sql.transfer](functions/transfer.md).

## Timing Logs

Public SQL operations log elapsed time and a final function-duration line.
Messages include structured context such as operation, connection, backend, and
phase so repeated details do not have to be embedded in every message body.
For `execute_read`, setup statement timings use the `setup` phase and the final
dataframe query uses `read`; the operation-level start and finish messages do
not repeat the operation name as a phase.

The timing sink can be routed through Python logging, which is usually a better
fit for Airflow task logs than direct printing. Logging records omit the
toolkit datetime prefix so logging handlers can render a single timestamp.

## Error Source Paths

For file-backed configuration, SQL failure diagnostics identify the full
absolute `.connections` path used by the operation. Configuration errors such
as an unknown connection key include it directly in the error message. Driver
and operation errors retain their original exception types and receive a
traceback note plus an error-level message through the timing sink:

```text
SQL connections file: /opt/project/.connections
```

On Python versions before 3.11, use the error-level message for displayed path
context; traceback notes remain available through the exception's `__notes__`.
Nested helpers avoid repeating the same diagnostic. The added diagnostic
contains the path only, and fileless Airflow operations omit unrelated file
paths. The path belongs to the [cached configuration source](configuration.md),
even if the file was removed after loading.

## Progress and Metadata

Progress bars are opt-in and best suited for interactive or long-running
loads/transfers. Metadata results are better for automation because they expose
row counts where available, elapsed seconds, retry attempts, status, and labels.

Batch helpers such as [sql.async_sql](functions/async_sql.md) and
[sql.parallel_sql](functions/parallel_sql.md) report task-level progress. Use
[sql.format_plan](functions/format_plan.md) when dry-run plans need readable
output.

[SQL module index](index.md)
