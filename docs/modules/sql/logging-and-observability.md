[SQL module index](index.md)

# Logging and Observability

SQL workflows emit timing and status information so database work can be traced
without printing every query by default.

## Query Visibility

Query text is hidden by default. Enable query printing only when reviewing SQL
locally or debugging a specific task. For scheduled jobs, prefer labels and
structured logs over full query text.

## Timing Logs

Public SQL operations log elapsed time and a final function-duration line.
Messages include structured context such as operation, connection, backend, and
phase so repeated details do not have to be embedded in every message body.

The timing sink can be routed through Python logging, which is usually a better
fit for Airflow task logs than direct printing.

## Progress and Metadata

Progress bars are opt-in and best suited for interactive or long-running
loads/transfers. Metadata results are better for automation because they expose
row counts where available, elapsed seconds, retry attempts, status, and labels.

[SQL module index](index.md)
