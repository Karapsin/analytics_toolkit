# Quick Start

Install the package:

```bash
pip install analytics-toolkit
```

## SQL Setup

Create a starter `.connections` file in the current working directory:

```python
from analytics_toolkit import sql

sql.generate_dummy_connections()
```

For Airflow DAGs, generate routing metadata instead:

```python
from analytics_toolkit import sql

sql.generate_dummy_connections(airflow=True)
```

The helper writes `./.connections` only when it does not already exist. Edit the
generated placeholders before running real SQL.

## General

```python
from analytics_toolkit.general import read_file, time_print

params = {"start_dt": "2026-03-01", "end_dt": "2026-03-31"}
query = read_file("queries/orders.sql", params_dict=params)
time_print("loaded SQL template")
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

## AB Utilities

```python
from analytics_toolkit import sql
from analytics_toolkit.ab_utils import RatioMetricSpec, compute_test_metrics

experiment_df = sql.read(
    "gp",
    """
    select
      user_id,
      group_name,
      orders,
      revenue,
      clicks,
      impressions
    from sandbox.experiment_metrics
    """,
)

result = compute_test_metrics(
    df=experiment_df,
    group="group_name",
    control="control",
    user_id="user_id",
    ratio_metrics=[
        RatioMetricSpec(
            name="ctr",
            numerator="clicks",
            denominator="impressions",
        )
    ],
)
```

## Dates

```python
from analytics_toolkit.dates.dates import first_day, gen_dates_list

month_start = first_day("2026-03-18", "month")
weeks = gen_dates_list("2026-03-01", "2026-03-31", interval="week")
```

## Excel

```python
from analytics_toolkit.excel import pivot_and_break_table

tables = pivot_and_break_table(
    df=report_df,
    rows="metric",
    columns="group_name",
    value="value",
    output="report.xlsx",
    prettify=True,
)
```
