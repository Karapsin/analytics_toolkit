# magnit_utils

`magnit_utils` is a small utility package for:

- SQL I/O and table-loading helpers for Trino, Greenplum, and ClickHouse
- date helpers for common period calculations
- Excel helpers for writing pivoted tables from long-format data

## Install

From the repository root:

```bash
pip install git+https://github.com/Karapsin/magnit_utils.git
```

## Quick Start

```python
from magnit_utils import sql
from magnit_utils.dates.dates import first_day
from magnit_utils.excel import break_into_tables
```

## Configuration

Connection settings are read from environment variables. By default the package looks
for a `.env` file starting from the current working directory and walking up through
its parents.

If `TRINO_USE_KEYCHAIN_CERTS=true`, the generated Trino CA bundle is written to:

- `<project_root>/certs/trino-keychain-ca.pem` when a `.env` file is found
- `<current_working_directory>/certs/trino-keychain-ca.pem` otherwise

You can override the env file path with `MAGNIT_UTILS_ENV_FILE` and the state/output
directory with `MAGNIT_UTILS_HOME`.

## Package Layout

- `magnit_utils/dates`: date and period helpers
- `magnit_utils/excel`: Excel formatting helpers
- `magnit_utils/sql`: SQL execution, loading, and transfer helpers
