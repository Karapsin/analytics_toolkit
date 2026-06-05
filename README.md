# analytics_toolkit

A compact Python toolkit for AB-test analysis, SQL workflows, Excel reports, and date helpers.

| Field | Value |
| --- | --- |
| Version | `1.3.6.13` |
| Python | `>=3.8,<3.15` |
| Install | `pip install analytics-toolkit` |
| License | MIT |
| Source | [github.com/Karapsin/analytics_toolkit](https://github.com/Karapsin/analytics_toolkit) |
| Issues | [GitHub Issues](https://github.com/Karapsin/analytics_toolkit/issues) |

## Imports

`analytics_toolkit` depends on `pandas`, `numpy`, `scipy`, `openpyxl`,
`sqlparse`, `sqlglot`, `trino`, `psycopg2-binary`, `clickhouse-connect`, and
related transport/compression helpers.

## Areas

- `analytics_toolkit.ab_utils`: AB-test metric comparison helpers.
- `analytics_toolkit.sql`: SQL read, execute, load, and transfer helpers.
- `analytics_toolkit.excel`: Excel report helpers for long-format dataframes.
- `analytics_toolkit.dates`: date and period helpers.
- `analytics_toolkit.general`: shared logging and file path helpers.

## SQL Import Policy

Supported SQL imports are `from analytics_toolkit import sql` or
`import analytics_toolkit.sql as sql`. Deep imports under
`analytics_toolkit.sql.*` are internal implementation details and may change;
call SQL helpers through the `sql` facade, for example `sql.create_sql_table(...)`
or `sql.transfer(...)`. Do not restore removed root implementation paths.

## Documentation

- [Full README](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/README_FULL.md)
- [analytics_toolkit Manual](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/ANALYTICS_TOOLKIT_MANUAL.md)
- [Airflow SQL Manual](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/AIRFLOW_SQL_MANUAL.md)
- [SQL module](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/sql.md)
- [AB utilities](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/ab_utils.md)
- [Excel helpers](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/excel.md)
- [Date helpers](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/modules/dates.md)
- [Changelog](https://github.com/Karapsin/analytics_toolkit/blob/main/docs/CHANGELOG.md)
