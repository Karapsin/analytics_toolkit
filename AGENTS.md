# AGENTS.md

## Scope

These instructions apply to the whole repository.

## Project Overview

`analytics_toolkit` is a Python 3.11+ utility package with five public areas:

- `analytics_toolkit.ab_utils`: AB-test metric comparison helpers.
- `analytics_toolkit.sql`: SQL read/execute/load/transfer helpers for Greenplum, Trino, and ClickHouse.
- `analytics_toolkit.excel`: long-format dataframe to Excel report helpers.
- `analytics_toolkit.dates`: date and period helpers.
- `analytics_toolkit.general`: shared logging and file path helpers.

Keep public APIs stable unless the user explicitly asks for a breaking change. Many tests import underscore helpers through package re-export modules, so treat exported internals as compatibility surface too.

## Development Commands

Use a temporary bytecode cache when running Python commands from this sandboxed workspace:

```bash
PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache python -m compileall analytics_toolkit tests
PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q
```

Focused test files:

```bash
PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_ab_utils_metrics.py
PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_excel_long_format.py
PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_general_read_file.py
PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_sql_connection_config.py tests/test_sql_retries.py tests/test_sql_load_table.py
```

CI matrix checks, required before every commit:

```bash
PYTHON38="$(pyenv prefix 3.8.18)/bin/python" \
PYTHON39="$(pyenv prefix 3.9.25)/bin/python" \
PYTHON310="$(pyenv prefix 3.10.20)/bin/python" \
PYTHON311="$(pyenv prefix 3.11.15)/bin/python" \
PYTHON312="$(pyenv prefix 3.12.13)/bin/python" \
tox -e py38-latest,py38-min,py39-latest,py310-latest,py311-latest,py312-latest
```

Run the full CI matrix locally before every commit. Do not commit unless all matrix environments pass; if an interpreter or dependency is missing, install it or explicitly report the blocker instead of skipping that environment.

Do not run tests against real databases. Unit tests should use fake connections, monkeypatching, and the autouse env fixture in `tests/conftest.py`.

## General Rules

- Prefer small, local changes that follow existing module patterns.
- Do not alter packaging metadata or rewrite README/manual docs unless the task requires it.
- After every non-documentation repository change, bump the package version in `pyproject.toml`. Documentation-only changes must not bump the package version. Versions use four parts: `a.b.c.d`, and each component has a maximum value of `19`. For a normal repository change, increment `d`; for example, `1.3.6.6` -> `1.3.6.7`. If `d` is already `19`, increment `c` and reset `d` to `0`; for example, `1.3.6.19` -> `1.3.7.0`. Apply the same carry rule to higher components: `1.3.19.19` -> `1.4.0.0`, `1.19.19.19` -> `2.0.0.0`. Do not let any component exceed `19`.
- When changing dependency declarations in `pyproject.toml`, update the CRAN-style `Depends`, `Imports`, and `Suggests` dependency entries in `README.md`.
- When changing public behavior, update the relevant module README and focused tests.
- When adding, removing, or renaming files under `docs/modules/<module>/`, update that module folder's `index.md` navigation in the same change.
- Non-index section files under `docs/modules/<module>/` should start and end with a link back to that module folder's `index.md`.
- In SQL module docs, describe general/public functions before backend-specific functions. Within each section, order functions by expected user frequency, with the more commonly used helper first.
- `docs/modules/sql/index.md` should contain only the link to `docs/modules/sql/functions/index.md`.
- SQL function docs live under `docs/modules/sql/functions/`. `functions/index.md` should start and end with a link back to `../index.md`, group general functions before backend-specific functions, and order likely high-frequency functions first in each section.
- Each SQL function page should start and end with a link back to `functions/index.md`, then include a brief function description, the exact `function_name(...inputs with defaults...)` signature, input descriptions, and optional notes. Prefer short public entrypoint pages such as `read.md`, `execute.md`, and `transfer.md` instead of duplicating long-form alias pages such as `read_sql.md`, `execute_sql.md`, or `transfer_table.md`.
- At the end of every non-documentation change, run the full local CI matrix from Development Commands before committing, even if focused tests were run earlier. For documentation-only changes, full checks are not required; run focused tests only when the documentation change affects tested paths or generated artifacts. Treat test failures and pytest warnings as issues to fix before finishing; the final test run should pass with no warning summary.
- Keep `.connections` out of the repo. Tests should create a temporary `.connections` and chdir into that temp project.
- Use existing structured parsers for SQL/table names (`sqlparse`, `sqlglot`) instead of ad hoc parsing where those modules already do the job.
- Once a coherent batch of changes is done, run `git add . && git commit -m '...'`, replacing `...` with a short description of the changes.

## PyPI Release Rules

When the user asks to update, publish, or release the package on PyPI, run the complete publishing workflow unless they explicitly ask for a narrower action:

- Publish the candidate version to TestPyPI first through GitHub Actions trusted publishing.
- Verify the TestPyPI artifact in a fresh temporary virtual environment from outside the repository checkout, and confirm imports resolve from that environment's `site-packages`.
- Publish the same production package/version to real PyPI through the GitHub release workflow.
- Verify the real PyPI artifact in a fresh temporary virtual environment from outside the repository checkout, and confirm imports resolve from that environment's `site-packages`.
- Check the GitHub Actions jobs after each publish. TestPyPI publishes must leave the real PyPI job skipped; real PyPI publishes must leave the TestPyPI job skipped.
- If TestPyPI uses a temporary project name, keep any package-name change on a temporary branch only and do not merge that branch into `main`.

## SQL Module Contracts

- Public SQL APIs accept connection keys/aliases from `.connections`; callers should not need to pass backend names separately.
- Each `.connections` value must include `type` as `gp`, `trino`, or `ch`. Backend dispatch comes from this `type`, while reconnect/retry/log messages keep using the alias key.
- Env-based SQL config such as `SQL_CONNECTIONS`, `GP_HOST`, `TRINO_HOST`, `CH_HOST`, `TRINO_INSERT_CHUNK_SIZE`, and config-file override env vars is intentionally unsupported. Do not restore fallback support.
- Keep public names such as `connection_type`, `from_db`, and `to_db` compatible even when they now represent aliases.
- A Trino target may define `insert_chunk_size` in its connection config. Explicit function arguments override config; config overrides the internal default.
- `read_sql`, `execute_sql`, `load_df`, and `transfer_table` retry the whole public operation with fresh connections. Preserve Greenplum rollback behavior on errors.
- `transfer_table` and `load_df` separate key and backend in option models. Same-backend aliases are valid as long as the alias keys differ.
- ClickHouse load/transfer creates and manages a shard table plus a `Distributed` table. Preserve local and cluster DDL/drop/truncate behavior.
- Key validation uses normalized unique key lists and null-safe joins for staged-vs-target overlap checks.
- Trino table metadata helpers need the alias key so unqualified names can use that connection's catalog/schema.

## SQL Layout Notes

- `connection/config.py`: finds `.connections`, parses it as JSON, normalizes aliases to lowercase, validates fields, and resolves alias to backend.
- `connection/get_sql_connection.py`: opens backend clients and handles optional Trino keychain certificate bundles under `MAGNIT_UTILS_HOME` or the env-file directory.
- `ddl/create_sql_table.py`: infers dataframe column types, quotes identifiers per backend, and builds ClickHouse distributed DDL.
- `dml/io`: read/execute helpers using `sqlparse`; `read_sql` accepts exactly one statement.
- `dml/load`: dataframe loading, stage table creation, batch insertion, Trino chunking, and backend-specific scalar normalization.
- `dml/table`: shared table existence, analyze, drop, vacuum, stage finalization, and validation helpers.
- `dml/transfer`: staged transfer flow, source streaming, full retry/restart behavior, and connection replacement helpers.

## AB Utilities Contracts

- `compute_test_metrics` expects one row per user, a non-null unique user id, a non-null group column, and at least one mean or ratio metric.
- Output column order is part of the API; preserve placement of `metric_type`, group columns, `p-value CUPED`, and `bootstrap_adj_p`.
- `analytics_toolkit.ab_utils.metrics` re-exports many underscore helpers. Tests may import those names directly.
- Ratio metrics support only `level="agg"` or `level="user"` and `invalid_denominator="ignore"`.
- Missing metric values are ignored per metric/group; non-numeric metric values should raise.
- CUPED failures should warn and return `NaN`, not abort the whole metric computation when validation has passed.
- Bootstrap multiple-comparison adjustment should remain deterministic when `bootstrap_random_state` is set and should fall back from process pools to threads when process pools are unavailable.

## Excel Contracts

- `pivot_and_break_table` and `break_table` accept either one dataframe or a sequence of dataframes.
- Preserve sheet grouping order, table order, side-by-side placement for multiple dataframes, and blank spacing between table blocks.
- Preserve sheet-name sanitization, 31-character truncation, and deduplication for append mode.
- Decimal values are coerced to floats before writing to Excel.
- `enforce_same_row_order=True` aligns later dataframe tables to the first dataframe and rejects extra row labels.

## Dates Contracts

- Date helpers accept ISO strings, `date`, or `datetime` values.
- The default return type is an ISO string; `output_string=False` returns midnight `datetime` values.
- Weekly and monthly sequences truncate start/end dates to the period start and emit warnings when truncation happens.
- `add_weeks` and `add_months` operate from the week/month start, not from the exact input day.

## General Module Contracts

- `time_print` prints timestamped messages and is re-exported through `analytics_toolkit.general` and `analytics_toolkit.sql`.
- `here()` prefers the caller's `__main__.__file__` directory, then falls back to the current working directory and unique cwd matches.
- `read_file()` raises `InvalidSqlInputError` for missing files and applies `str.format(**params_dict)` only when params are provided.
- Preserve the `analytics_toolkit.general.read_file.inspect` compatibility assignment; tests monkeypatch through that dotted path.
