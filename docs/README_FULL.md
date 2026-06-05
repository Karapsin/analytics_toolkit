# analytics_toolkit

`analytics_toolkit` is a small utility package for:

- AB-test related helpers
- SQL I/O and table-loading helpers for Trino, Greenplum, and ClickHouse
- date helpers for common period calculations
- Excel helpers for writing pivoted tables from long-format data

## Install

From PyPI:

```bash
pip install analytics-toolkit
```

From GitHub:

```bash
pip install git+https://github.com/Karapsin/analytics_toolkit.git
```

## Release

PyPI publishing uses GitHub Actions trusted publishing. Before each release,
run the full local tox matrix from `AGENTS.md`, update the changelog, and verify
the package build:

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
```

Run the `publish` workflow manually to publish the current version to TestPyPI.
After the TestPyPI install check passes, create and publish a GitHub release
tagged as `v<pyproject.toml version>`; the release event publishes to PyPI.
The PyPI trusted publisher uses repository `Karapsin/analytics_toolkit`,
workflow `.github/workflows/publish.yml`, and environment `pypi`. The TestPyPI
trusted publisher uses the same repository and workflow with environment
`testpypi`.

## Quick Start

```python
from analytics_toolkit.ab_utils import compute_test_metrics
from analytics_toolkit import sql
from analytics_toolkit.dates.dates import first_day
from analytics_toolkit.excel import break_table, pivot_and_break_table
```

Supported SQL imports are `from analytics_toolkit import sql` or
`import analytics_toolkit.sql as sql`. Deep imports under
`analytics_toolkit.sql.*` are internal implementation details and may change;
call SQL helpers through the `sql` facade, for example `sql.create_sql_table(...)`
or `sql.transfer(...)`. Do not restore removed root implementation paths.

## Configuration

SQL connection settings are read from `.connections`. The package searches
from the current working directory upward through parent directories. Each key is
the public connection alias used by `analytics_toolkit.sql`; each value must
include `type` as one of `gp`, `trino`, or `ch`.

Use `sql.generate_dummy_connections()` to create a starter direct
`./.connections` file in the current working directory. Use
`sql.generate_dummy_connections(airflow=True)` for an Airflow-source starter
file. Existing files are not overwritten.

```json
{
  "gp": {
    "type": "gp",
    "host": "gp.example",
    "port": 5432,
    "user": "user",
    "password": "password",
    "database": "db"
  },
  "gp_sandbox": {
    "type": "gp",
    "host": "gp-sandbox.example",
    "user": "user",
    "password": "password",
    "database": "sandbox"
  }
}
```

Connection settings live in `.connections`. Environment variables such as
`GP_HOST`, `TRINO_HOST`, `CH_HOST`, `SQL_CONNECTIONS`, and
`TRINO_INSERT_CHUNK_SIZE` are not supported; Trino insert chunk sizing is the
Trino connection field `insert_chunk_size`.

If a Trino connection sets `use_keychain_certs=true`, the generated CA bundle is
written to:

- `<connections_file_directory>/certs/trino-<connection-key>-keychain-ca.pem`

You can override the state/output directory with `MAGNIT_UTILS_HOME`.

## Manuals

- [analytics_toolkit Manual](ANALYTICS_TOOLKIT_MANUAL.md)
- [Airflow SQL Manual](AIRFLOW_SQL_MANUAL.md)

## Package Layout

- `analytics_toolkit/ab_utils`: AB-test metric comparison helpers, including `compute_test_metrics`
- `analytics_toolkit/dates`: date and period helpers
- `analytics_toolkit/excel`: Excel formatting helpers
- `analytics_toolkit/sql`: SQL execution, loading, and transfer helpers
