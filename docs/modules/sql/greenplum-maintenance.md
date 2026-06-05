[SQL module index](index.md)

# Greenplum Maintenance

Use `gp_vacuum` for Greenplum vacuum operations that must run outside a transaction
block.

```python
from analytics_toolkit import sql

sql.gp_vacuum("sandbox.some_table")
sql.gp_vacuum("sandbox.some_table", analyze=True)
sql.gp_vacuum("sandbox.some_table", full=True, verbose=True)
```

Use `gp_cancel_all_running_queries` to cancel every current-user Greenplum
backend PID returned from `pg_stat_activity`, excluding the caller session. It
returns one row per PID with the generated `pg_cancel_backend` query and the
boolean cancellation result. Set `concurrency` above `1` to run cancellation
queries in parallel with separate fresh connections.

```python
cancelled = sql.gp_cancel_all_running_queries("gp", concurrency=4)
```

[SQL module index](index.md)
