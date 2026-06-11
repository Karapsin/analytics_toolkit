[SQL module index](index.md)

# Parallel Workflows

Parallel workflows group independent SQL tasks and run them with controlled
concurrency. They are useful for fan-out reads, independent refreshes, and
multi-step jobs where some steps can proceed while others wait on database I/O.
Use [sql.async_sql](functions/async_sql.md) or
[sql.parallel_sql](functions/parallel_sql.md) for these batches.

## Task Batches

Task specs describe the operation type and the same arguments that would be
passed to the matching synchronous helper, such as
[sql.read](functions/read.md), [sql.execute](functions/execute.md),
[sql.execute_read](functions/execute_read.md),
[sql.load_df](functions/load_df.md), or [sql.transfer](functions/transfer.md).
Named tasks make result dictionaries stable and easier to inspect.

Use fail-fast behavior when one failed task invalidates the batch. Disable it
when partial results are acceptable and failures should be reported per task.

## Date-Partitioned Batches

Use [gen_dates_list](../dates/functions/gen-dates-list.md) to create one task
per reporting period, then use the matching date offset helper to build each
task's exclusive upper bound.

```python
from analytics_toolkit import sql
from analytics_toolkit.dates import add_months, gen_dates_list

target_table = "sandbox.monthly_order_metrics"
start_month = "2026-01-01"
end_month = "2026-04-01"

tasks = []
for month_start in gen_dates_list(
    start_month,
    add_months(end_month, -1),
    interval="months",
):
    month_end = add_months(month_start, 1)
    tasks.append(
        {
            "name": f"load_{month_start[:7]}",
            "type": "execute",
            "db_key": "gp",
            "query": f"""
                insert into {target_table} (event_month, users)
                select
                  date '{month_start}' as event_month,
                  count(distinct user_id) as users
                from sandbox.events
                where event_ts >= timestamp '{month_start}'
                  and event_ts < timestamp '{month_end}'
                group by 1
            """,
        }
    )

sql.parallel_sql(tasks, concurrency=3)
```

## Concurrency Caps

Requested concurrency describes how much work the caller wants. Soft caps can
lower actual worker execution without changing the requested batch shape. Hard
caps protect the process from accidentally launching too many workers.

## Pipelines

Custom pipelines keep ordered Python steps inside one task while other tasks
continue under the outer concurrency limit. Use them for small conditional
flows, such as reading a source count before deciding whether to transfer data.

[SQL module index](index.md)
