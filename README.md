# analytics_toolkit

Python toolkit for AB-test analysis, SQL workflows, Excel reports, and date helpers.

[Quick Start Guide](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/QUICK_START.md)

[Documentation Overview](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/README.md)

**Version:** `1.3.11.17`<br>
**Depends:** Python (`>=3.8,<3.15`)<br>
**Imports:** [clickhouse-connect](https://pypi.org/project/clickhouse-connect/) (`>=0.5.14,<1`), [fsspec](https://pypi.org/project/fsspec/) (`>=2024.2`), [lz4](https://pypi.org/project/lz4/) (`>=4.3.2,<5`), [numpy](https://pypi.org/project/numpy/) (`>=1.24.2,<2`), [openpyxl](https://pypi.org/project/openpyxl/) (`>=3.1.1,<4`), [orjson](https://pypi.org/project/orjson/) (`>=3.8.7,<4`), [pandas](https://pypi.org/project/pandas/) (`>=1.4.4,<3`), [psycopg2-binary](https://pypi.org/project/psycopg2-binary/) (`>=2.9.5,<3`), [pyarrow](https://pypi.org/project/pyarrow/) (`>=14,<23`), [python-dateutil](https://pypi.org/project/python-dateutil/) (`>=2.8.2,<3`), [pytz](https://pypi.org/project/pytz/) (`>=2022.7`), [requests](https://pypi.org/project/requests/) (`>=2.28.2,<3`), [s3fs](https://pypi.org/project/s3fs/) (`>=2024.2`), [scipy](https://pypi.org/project/scipy/) (`>=1.10.1,<2`), [sqlglot](https://pypi.org/project/sqlglot/) (`>=26.33,<31`), [sqlparse](https://pypi.org/project/sqlparse/) (`>=0.4.3,<1`), [tqdm](https://pypi.org/project/tqdm/) (`>=4.65.0,<5`), [trino](https://pypi.org/project/trino/) (`>=0.320,<1`), [zstandard](https://pypi.org/project/zstandard/) (`>=0.20.0,<1`)<br>
**Suggests:** [apache-airflow](https://pypi.org/project/apache-airflow/) (`>=2.4,<3`; optional extra `airflow`), [clickhouse-driver](https://pypi.org/project/clickhouse-driver/) (`>=0.2.9,<0.2.10; python_version == '3.8'`; optional extra `clickhouse-native`), [clickhouse-driver](https://pypi.org/project/clickhouse-driver/) (`>=0.2.10,<1; python_version >= '3.9'`; optional extra `clickhouse-native`), [pyperclip](https://pypi.org/project/pyperclip/) (`>=1.11,<2`; optional extra `tui`), [textual](https://pypi.org/project/textual/) (`>=0.73,<0.74; python_version < '3.13'`; optional extra `tui`), [textual](https://pypi.org/project/textual/) (`[syntax]>=0.89.1,<0.90; python_version >= '3.13'`; optional extra `tui`), [tree-sitter](https://pypi.org/project/tree-sitter/) (`>=0.20.1,<0.21.0; python_version < '3.13'`; optional extra `tui`), [tree-sitter](https://pypi.org/project/tree-sitter/) (`>=0.23,<0.24; python_version >= '3.13'`; optional extra `tui`), [tree-sitter-languages](https://pypi.org/project/tree-sitter-languages/) (`==1.10.2; python_version < '3.13'`; optional extra `tui`), [tree-sitter-sql](https://pypi.org/project/tree-sitter-sql/) (`>=0.3,<0.3.8; python_version >= '3.13'`; optional extra `tui`)<br>
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

The normalized underscore spelling installs the same distribution:

```bash
pip install analytics_toolkit
```

From GitHub:

```bash
pip install git+https://github.com/Karapsin/analytics_toolkit.git
```

## Convenience Import

The installed distribution provides `atk` as an optional shortcut for common
notebook and analytics imports:

```python
import atk

frame = atk.pd.DataFrame({"value": [1, 2, 3]})
today = atk.dt.get_today()
```

It exposes `pd`, `sql`, `dt`, `dttm`, `ab`, `sql_format`, `excel`, `here`,
`read_file_here`, `time_print`, `from_here`, `get_time_print_sink`,
`set_connections_path`, and `write_file` as direct aliases to the corresponding
pandas and `analytics_toolkit` objects.

## Areas

- `analytics_toolkit.ab_utils`: AB-test metric comparison helpers.
- `analytics_toolkit.sql`: SQL read, execute, load, and transfer helpers.
- `analytics_toolkit.sql_explorer`: optional exploratory SQL terminal interface.
- `analytics_toolkit.sql_format`: SQL formatting, CTE rewrite, and Greenplum temp-table rewrite helpers.
- `analytics_toolkit.excel`: Excel report helpers for long-format dataframes.
- `analytics_toolkit.dates`: date and period helpers.
- `analytics_toolkit.datetime`: timestamp helpers that preserve time components.
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
- `sql.execute`: run one DDL/DML string or a concurrent list of independent queries.
- `sql.execute_read`: run setup SQL and return the final result as a dataframe.
- `sql.insert`: insert one query result into an existing table by column position.
- `sql.execute_insert`: run setup SQL and insert the final query result.
- `sql.execute_create`: run setup SQL and create a table from the final query.
- `sql.create_table`: create a table from a dataframe, schema, or query.
- `sql.load_df`: load a pandas dataframe into a configured backend table.
- `sql.transfer`: move rows from a source query to a target table across backends.

## Exploratory SQL TUI

Install the optional terminal dependencies and open a configured connection by
its `.connections` key:

```bash
pip install 'analytics-toolkit[tui]'
analytics-toolkit sql explore gp
```

Run `analytics-toolkit sql explore` without a key to select a valid configured
connection inside the terminal first.

The same interface can be launched from a terminal Python or IPython console:

```python
from analytics_toolkit import sql_explorer

sql_explorer.run("gp")
```

The SQL editor and text inputs use steady, non-blinking carets. The numbered
editor has standard, non-modal shortcuts. `Ctrl+Enter` and `F5` are the portable
run keys; multiple non-empty selections are joined in document order instead of
running only one. Terminal-forwarded Command/Fn-like chords share Explorer Ctrl
shortcuts, though terminals may intercept them. `Ctrl+T`, `Ctrl+W`, and the
Ctrl+Tab variants manage independent `[db] file.sql` workspace tabs. Press
`Ctrl+O` (or forwarded `Cmd+O`) to enter
read-only navigation mode on the host where the Explorer is running. Its path
input supports Tab completion and keyboard candidate choice. All in-root files
are visible for orientation, but only `.sql` files can be opened. Over SSH,
this is the remote host's filesystem.

`Tab` inserts a sole completion directly, opens the menu for multiple local SQL
or backend metadata matches, and otherwise indents. Built-in SQL keywords are
lower case. Keep typing while the menu is open to narrow its options. A table
lookup starts only after six prefix characters, calls `sql.show_tables` once
for that context, and filters longer or shortened prefixes locally. Results
use comma thousands separators for numeric values and support rectangular
selection with visible-value TSV copying. Use `to_excel` or `to_csv` in the
command panel to choose a project directory and save the current result without
its dataframe index. `Ctrl+C` emits
OSC 52 first so an SSH client terminal can copy into its local clipboard;
terminal policy may disable OSC 52, in which case Pyperclip or the in-memory
fallback remains available. Use the `Interrupt` button or the `cancel` command
for the active tab's user query. User SQL has one shared FIFO queue per database,
with at most one active user query on each database; metadata has its own
separate shared queue per database.
See the
[SQL explorer guide](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/sql_explorer/index.md)
for navigation, completion, clipboard, and safety details.

## SQL Formatting

[SQL formatting guide](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/sql_format/index.md)

`sql_format.format_sql`, `sql_format.rewrite_with_ctes`, and
`sql_format.gp_rewrite_to_temp_tables` transform SQL text locally without
opening database connections. `GROUP BY` and `ORDER BY` clauses use SELECT-list
ordinals by default, with expression-based formatting available through
`group_by_format="expressions"` and `order_by_format="expressions"`.

```python
from analytics_toolkit import sql_format

formatted = sql_format.format_sql(
    "select user_id, amount from orders where amount > 100",
    dialect="postgres",
)
```

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

| metric_type | group_1 | group_2 | metric_name | n_group_1 | n_group_2 | outliers_cutoff | outliers_n_group_1 | outliers_n_group_2 | metric_group_1 | metric_group_2 | variance_group_1 | variance_group_2 | delta_abs | delta_relative | mde_abs | mde_relative | s.e. | p-value | s.e. CUPED | p-value CUPED | mde_abs CUPED | mde_relative CUPED | s.e. bootstrap | bootstrap_adj_p |
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

## Datetime Helpers

[All datetime functions](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/datetime/functions/index.md)

[Datetime helpers guide](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/datetime/index.md)

Datetime helpers preserve timestamp components for second-level reporting,
windowing, and scheduling workflows. Use them when calendar date truncation from
`analytics_toolkit.dates` is not desired.

```python
from analytics_toolkit import datetime as dttm

next_run = dttm.add_days("2026-01-01 12:13:15", 1)
# "2026-01-02 12:13:15"

hour_window = dttm.datetime_bounds("2026-01-01 12:13:15", period="hour")
# ("2026-01-01 12:00:00", "2026-01-01 12:59:59")
```

- `add_seconds`, `add_minutes`, `add_hours`, `add_days`, `add_weeks`, `add_months`, `add_quarters`: shift timestamps.
- `datetime_bounds`: get minute, hour, day, week, month, or quarter timestamp boundaries.
- `gen_datetimes_list`: build timestamp sequences.
- `format_datetime` / `sanitize_datetime`: format timestamps for display, SQL, or filenames.

## Documentation

- [Documentation Overview](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/README.md)
- [Quick Start](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/QUICK_START.md)
- [Airflow SQL Manual](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/AIRFLOW_SQL_MANUAL.md)
- [Module documentation](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/README.md)
- [Changelog](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/CHANGELOG.md)
