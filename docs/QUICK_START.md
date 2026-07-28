[Documentation overview](README.md)

# Quick Start

Install the package:

```bash
pip install analytics-toolkit
```

## SQL Setup

Create a starter `.connections` file and local `.certs/` directory in the
current working directory:

```python
from analytics_toolkit import sql

connections_path = sql.generate_dummy_connections()
print(connections_path)
# /path/to/your/project/.connections
```

The generated direct file has one starter entry per supported backend, including
recommended DDL and transfer-staging defaults. Replace its example values before
use. A compact direct configuration has this shape:

```json
{
  "gp": {
    "type": "gp",
    "host": "gp.example",
    "port": 5432,
    "user": "user",
    "password": "password",
    "database": "analytics"
  },
  "trino": {
    "type": "trino",
    "host": "trino.example",
    "port": 8080,
    "user": "user",
    "password": "password",
    "catalog": "iceberg",
    "schema": "analytics",
    "http_scheme": "https"
  },
  "ch": {
    "type": "ch",
    "host": "ch.example",
    "port": 8123,
    "user": "user",
    "password": "password",
    "database": "analytics",
    "secure": true
  }
}
```

The generated file also includes example certificate references. Put retained
certificate files in `.certs/`, or remove those options when custom CA files are
not required. DDL defaults, staging settings, certificates, aliases, and all
other supported options are described in the
[SQL configuration guide](modules/sql/configuration.md).

For Airflow DAGs, generate routing metadata instead:

```python
from analytics_toolkit import sql

sql.generate_dummy_connections(airflow=True)
```

The helper writes `./.connections` only when it does not already exist and
creates `./.certs/` for local certificate files. Edit the generated
placeholders before running real SQL.

## General

Use `read_file` to keep long SQL templates out of Python code while still
making runtime parameters explicit:

```python
from analytics_toolkit import sql
from analytics_toolkit.general import read_file

params = {"start_dt": "2026-03-01", "end_dt": "2026-03-31"}
query = read_file("queries/orders_by_day.sql", params_dict=params)
df = sql.read("gp", query)
```

## SQL

```python
from analytics_toolkit import sql

sql.execute(
    "gp",
    """
    insert into sandbox.orders
    select user_id, count(*) as orders
    from sandbox.raw_orders
    group by user_id
    """,
)
df = sql.read("gp", "select * from sandbox.orders limit 10")
```

Use `execute_read` to run setup statements and read the final result in one
call:

```python
df = sql.execute_read(
    "gp",
    """
    insert into sandbox.orders
    select user_id, count(*) as orders
    from sandbox.raw_orders
    group by user_id;

    select * from sandbox.orders limit 10
    """,
)
```

Use `transfer` to stream a query result from one configured connection into a
table on another connection:

```python
rows = sql.transfer(
    from_db="trino",
    to_db="gp",
    from_sql="""
    select
      order_date,
      count(*) as order_count,
      sum(order_amount) as revenue
    from sandbox.raw_orders
    where order_date >= date '2026-03-01'
      and order_date < date '2026-04-01'
    group by order_date
    """,
    to_table="sandbox.daily_order_metrics",
    write_mode="replace",
    batch_size=50_000,
)
```

Use [sql.load_df](modules/sql/functions/load_df.md) when Python already owns the
rows. Columns containing Python `uuid.UUID` values infer as native `UUID` on
Greenplum, Trino, and ClickHouse; ClickHouse uses `Nullable(UUID)` when the
column contains nulls:

```python
from uuid import uuid4

import pandas as pd

events = pd.DataFrame(
    {
        "event_id": [uuid4(), uuid4()],
        "event_name": ["checkout_started", "order_completed"],
    }
)

inserted = sql.load_df(
    "ch",
    "quick_start_events",
    events,
    write_mode="replace",
    order_by="event_id",
    ch_engine="MergeTree",
    ch_only_shard=True,
)
print(inserted)
# 2
```

## AB Utilities

```python
from analytics_toolkit import sql
from analytics_toolkit.ab_utils import compute_test_metrics

experiment_df = sql.read(
    "gp",
    """
    select
      user_id,
      group_name,
      orders,
      revenue,
      clicks,
      views
    from sandbox.experiment_metrics
    """,
)

result = compute_test_metrics(
    df=experiment_df,
    group="group_name",
    control="control",
    user_id="user_id",
    ratio_metrics=[
        {"name": "ctr", "numerator": "clicks", "denominator": "views"},
    ],
)
```

## Dates

```python
from analytics_toolkit.dates import first_day, gen_dates_list

month_start = first_day("2026-03-18", "month")
weeks = gen_dates_list("2026-03-02", "2026-03-30", interval="weeks")
```

Date lists are useful for batch SQL jobs where the same query runs once per
period. Both endpoints are included. Weekly and monthly sequences normalize
their inputs to period starts and warn when an input is not already aligned.

For a daily job using half-open SQL windows, keep this template in
`queries/load_daily_order_metrics.sql`:

```sql
insert into sandbox.daily_order_metrics (dt, order_count, revenue)
select
  date '{start_dt}' as dt,
  count(*) as order_count,
  sum(order_amount) as revenue
from sandbox.raw_orders
where order_created_at >= timestamp '{start_dt}'
  and order_created_at < timestamp '{end_dt}'
group by 1
```

Build one task per day and run up to five tasks at once:

```python
from analytics_toolkit import sql
from analytics_toolkit.dates import add_days, gen_dates_list
from analytics_toolkit.general import read_file

tasks = []
for start_dt in gen_dates_list("2026-03-01", "2026-03-31", interval="day"):
    end_dt = add_days(start_dt, 1)
    query = read_file(
        "queries/load_daily_order_metrics.sql",
        params_dict={"start_dt": start_dt, "end_dt": end_dt},
    )
    tasks.append(
        {
            "name": f"load_{start_dt}",
            "type": "execute",
            "db_key": "gp",
            "query": query,
        }
    )

results = sql.parallel_sql(tasks, concurrency=5)
print(len(results))
# 31
```

The default hard concurrency cap is `5`. Use `soft_concurrency_cap` to throttle
a larger task graph below its requested concurrency, or pass an explicit higher
`hard_concurrency_cap` when the database and client are sized for it. See the
[parallel SQL workflow guide](modules/sql/parallel-workflows.md) for task types,
nested batches, progress, and failure handling.

## Excel

Use Excel helpers at the end of a report pipeline. This example reads a SQL
template that returns one row per user, computes AB metrics, formats report
tables, and writes them side by side:

```python
from analytics_toolkit import ab_utils as ab
from analytics_toolkit import excel, sql
from analytics_toolkit.general import read_file

query = read_file(
    "queries/experiment_user_metrics.sql",
    params_dict={"start_dt": "2026-03-01", "end_dt": "2026-03-31"},
)
experiment_df = sql.read("gp", query)

metrics_df = ab.compute_test_metrics(
    df=experiment_df,
    group="group_name",
    control="control",
    user_id="user_id",
    ratio_metrics=[
        {"name": "ctr", "numerator": "clicks", "denominator": "views"},
    ],
)

values_df = ab.format_ab_metrics(metrics_df, output_type="metric_values")
uplifts_df = ab.format_ab_metrics(
    metrics_df,
    output_type="delta_relative_significant",
    significance_alpha=0.05,
    significance_p_value="p_values",
)

tables = excel.break_table(
    df=[values_df, uplifts_df],
    output="experiment_report.xlsx",
    prettify=True,
)
```

[Documentation overview](README.md)
