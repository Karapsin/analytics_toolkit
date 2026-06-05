# analytics_toolkit

A compact Python toolkit for AB-test analysis, SQL workflows, Excel reports, and date helpers.

| Field | Value |
| --- | --- |
| Version | `1.3.6.13` |
| Depends | Python (`>=3.8,<3.15`) |
| Imports | [clickhouse-connect](https://pypi.org/project/clickhouse-connect/) (`>=0.5.14,<1`), [lz4](https://pypi.org/project/lz4/) (`>=4.3.2,<5`), [numpy](https://pypi.org/project/numpy/) (`>=1.24.2,<2`), [openpyxl](https://pypi.org/project/openpyxl/) (`>=3.1.1,<4`), [orjson](https://pypi.org/project/orjson/) (`>=3.8.7,<4`), [pandas](https://pypi.org/project/pandas/) (`>=1.4.4,<3`), [psycopg2-binary](https://pypi.org/project/psycopg2-binary/) (`>=2.9.5,<3`), [python-dateutil](https://pypi.org/project/python-dateutil/) (`>=2.8.2,<3`), [pytz](https://pypi.org/project/pytz/) (`>=2022.7`), [requests](https://pypi.org/project/requests/) (`>=2.28.2,<3`), [scipy](https://pypi.org/project/scipy/) (`>=1.10.1,<2`), [sqlglot](https://pypi.org/project/sqlglot/) (`>=20,<31`), [sqlparse](https://pypi.org/project/sqlparse/) (`>=0.4.3,<1`), [tqdm](https://pypi.org/project/tqdm/) (`>=4.65.0,<5`), [trino](https://pypi.org/project/trino/) (`>=0.320,<1`), [zstandard](https://pypi.org/project/zstandard/) (`>=0.20.0,<1`) |
| Suggests | [apache-airflow](https://pypi.org/project/apache-airflow/) (`>=2.4,<3`; optional extra `airflow`) |
| Install | `pip install analytics-toolkit` |
| License | MIT |
| Source | [github.com/Karapsin/analytics_toolkit](https://github.com/Karapsin/analytics_toolkit) |
| Issues | [GitHub Issues](https://github.com/Karapsin/analytics_toolkit/issues) |

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
