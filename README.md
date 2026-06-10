# analytics_toolkit

Python toolkit for AB-test analysis, SQL workflows, Excel reports, and date helpers.

[Quick Start Guide](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/QUICK_START.md)

[Documentation Overview](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/README.md)

**Version:** `1.3.8.14`<br>
**Depends:** Python (`>=3.8,<3.15`)<br>
**Imports:** [clickhouse-connect](https://pypi.org/project/clickhouse-connect/) (`>=0.5.14,<1`), [lz4](https://pypi.org/project/lz4/) (`>=4.3.2,<5`), [numpy](https://pypi.org/project/numpy/) (`>=1.24.2,<2`), [openpyxl](https://pypi.org/project/openpyxl/) (`>=3.1.1,<4`), [orjson](https://pypi.org/project/orjson/) (`>=3.8.7,<4`), [pandas](https://pypi.org/project/pandas/) (`>=1.4.4,<3`), [psycopg2-binary](https://pypi.org/project/psycopg2-binary/) (`>=2.9.5,<3`), [python-dateutil](https://pypi.org/project/python-dateutil/) (`>=2.8.2,<3`), [pytz](https://pypi.org/project/pytz/) (`>=2022.7`), [requests](https://pypi.org/project/requests/) (`>=2.28.2,<3`), [scipy](https://pypi.org/project/scipy/) (`>=1.10.1,<2`), [sqlglot](https://pypi.org/project/sqlglot/) (`>=20,<31`), [sqlparse](https://pypi.org/project/sqlparse/) (`>=0.4.3,<1`), [tqdm](https://pypi.org/project/tqdm/) (`>=4.65.0,<5`), [trino](https://pypi.org/project/trino/) (`>=0.320,<1`), [zstandard](https://pypi.org/project/zstandard/) (`>=0.20.0,<1`)<br>
**Suggests:** [apache-airflow](https://pypi.org/project/apache-airflow/) (`>=2.4,<3`; optional extra `airflow`)<br>
**Install:** `pip install analytics-toolkit`<br>
**PyPI:** [pypi.org/project/analytics-toolkit](https://pypi.org/project/analytics-toolkit/)<br>
**License:** MIT<br>
**Source:** [github.com/Karapsin/analytics_toolkit](https://github.com/Karapsin/analytics_toolkit)<br>
**Issues:** [GitHub Issues](https://github.com/Karapsin/analytics_toolkit/issues)

## Installation

From PyPI:

```bash
pip install analytics-toolkit
```

From GitHub:

```bash
pip install git+https://github.com/Karapsin/analytics_toolkit.git
```

## Areas

- `analytics_toolkit.ab_utils`: AB-test metric comparison helpers.
- `analytics_toolkit.sql`: SQL read, execute, load, and transfer helpers.
- `analytics_toolkit.excel`: Excel report helpers for long-format dataframes.
- `analytics_toolkit.dates`: date and period helpers.
- `analytics_toolkit.general`: shared logging and file path helpers.

## SQL Workflows

[All SQL functions](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/sql/functions/index.md)

[SQL module guide](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/sql/index.md)

`sql.transfer` streams query results between configured SQL backends, with
batching, retries, and table creation or replacement handled by one call.

```python
from analytics_toolkit import sql

rows = sql.transfer(
    from_db="trino",
    to_db="gp",
    from_sql="select user_id, order_id, amount from iceberg.analytics.orders",
    to_table="sandbox.orders_copy",
    write_mode="replace",
    batch_size=50_000,
    progress=True,
)
```

Aliases can point to the same backend type, so Greenplum-to-Greenplum transfers
work the same way.

```python
rows = sql.transfer(
    from_db="gp_sales",
    to_db="gp_finance",
    from_sql="select user_id, order_id, amount from mart.sales_orders",
    to_table="finance.sales_orders_copy",
    write_mode="replace",
    batch_size=50_000,
)
```

- `sql.read`: run a query and return a dataframe.
- `sql.execute`: run DDL or DML without returning a dataframe.
- `sql.execute_read`: run setup SQL and return the final result as a dataframe.
- `sql.load_df`: load a pandas dataframe into a configured backend table.
- `sql.transfer`: move rows from a source query to a target table across backends.

## AB Metrics

[All AB functions](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/ab_utils/functions/index.md)

[AB utilities guide](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/ab_utils/index.md)

`compute_test_metrics` compares experiment groups across mean and ratio metrics,
with optional CUPED statistics and bootstrap multiple-comparison adjustment.

```python
from analytics_toolkit.ab_utils import compute_test_metrics

result = compute_test_metrics(
    experiment_df,
    group="group_name",
    control="control",
    user_id="user_id",
    ratio_metrics=[
        {"name": "ctr", "numerator": "clicks", "denominator": "views"},
    ],
    pre_exp_metrics_df=pre_experiment_df,
    multiple_comparisons_adjustment=True,
    multiple_comparisons_adjustment_resamples=1000,
)
```

Example output with CUPED and bootstrap columns enabled:

| metric_type | group_1 | group_2 | metric_name | n0 | n1 | outliers_cutoff | outliers_n_control | outliers_n_test | metric_control | metric_test | variance_control | variance_test | delta_abs | delta_relative | mde_abs | mde_relative | s.e. | p-value | s.e. CUPED | p-value CUPED | mde_abs CUPED | mde_relative CUPED | s.e. bootstrap | bootstrap_adj_p |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| mean | test | control | revenue | 10000 | 10050 | 250.0 | 3 | 4 | 12.40 | 13.10 | 45.20 | 47.80 | 0.70 | 0.056 | 0.42 | 0.034 | 0.15 | 0.003 | 0.11 | 0.001 | 0.31 | 0.025 | 0.14 | 0.012 |
| ratio | test | control | ctr | 10000 | 10050 | 1.0 | 0 | 0 | 0.082 | 0.087 | 0.0009 | 0.0010 | 0.005 | 0.061 | 0.003 | 0.037 | 0.001 | 0.008 | 0.001 | 0.006 | 0.002 | 0.024 | 0.001 | 0.019 |

## Date Helpers

[All date functions](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/dates/functions/index.md)

[Date helpers guide](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/dates/index.md)

Date helpers cover reporting ranges, period boundaries, offsets, and stable
string formatting for SQL and filenames.

```python
from analytics_toolkit.dates import add_days, first_day, gen_dates_list, last_day

report_days = gen_dates_list("2026-06-01", "2026-06-07")
# ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05", "2026-06-06", "2026-06-07"]

month_start = first_day("2026-06-08", "month")
# "2026-06-01"

month_end = last_day("2026-06-08", "month")
# "2026-06-30"

next_run = add_days("2026-06-08", 1)
# "2026-06-09"
```

- `gen_dates_list`: build daily, weekly, monthly, or quarterly sequences.
- `first_day` / `last_day`: get week, month, or quarter boundaries.
- `add_days`, `add_weeks`, `add_months`, `add_quarters`: shift dates.
- `sanitize_date`: convert a date to compact `YYYYMMDD` text.

## Documentation

- [Documentation Overview](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/README.md)
- [Quick Start](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/QUICK_START.md)
- [Airflow SQL Manual](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/AIRFLOW_SQL_MANUAL.md)
- [Module documentation](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/README.md)
- [Changelog](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/CHANGELOG.md)
