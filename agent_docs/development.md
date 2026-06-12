# Development Agent Instructions

Read this file for implementation, testing, build, or commit work.

## Development Commands

Use release routines for repository-wide checks and releases. Run pre-commit checks with:

```bash
release_routines/pre_commit_checks.sh
```

The pre-commit script uses a temporary bytecode cache, runs compileall and pytest,
and then runs the full tox matrix for Python 3.8 through 3.14 plus the Python
3.8 minimum-dependency environment.

Use a temporary bytecode cache when running focused Python commands from this
sandboxed workspace:

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

Run `release_routines/pre_commit_checks.sh` before every commit. Do not commit
unless all matrix environments pass; if an interpreter or dependency is missing,
install it or explicitly report the blocker instead of skipping that environment.

Do not run tests against real databases. Unit tests should use fake connections,
monkeypatching, and the autouse env fixture in `tests/conftest.py`.
