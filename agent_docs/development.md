# Development Agent Instructions

Read this file for implementation, testing, build, or commit work.

## Development Commands

Use `workflow_status(...)` to get the required instruction files, metadata
status, and recommended checks for the current task. Use
`run_checks(area=..., level="focused")` for focused validation and
`run_checks(level="precommit")` before every commit.

The pre-commit check uses a temporary bytecode cache, runs compileall and
pytest, and then runs the full tox matrix for Python 3.8 through 3.14 plus the
Python 3.8 minimum-dependency environment. Do not commit unless all matrix
environments pass; if an interpreter or dependency is missing, install it or
explicitly report the blocker instead of skipping that environment.

Do not run tests against real databases. Unit tests should use fake connections,
monkeypatching, and the autouse env fixture in `tests/conftest.py`.
