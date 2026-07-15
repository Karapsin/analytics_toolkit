# Development Agent Instructions

Read this file for implementation, testing, build, or commit work.

## Development Commands

Use `workflow_status(...)` to get the required instruction files, metadata
status, and recommended checks for the current task. Use
`run_checks(area=..., level="focused")` for focused validation and
`run_checks(level="precommit")` before every commit.

The pre-commit check uses a temporary bytecode cache, runs metadata and minimum
constraint validation, compileall, and pytest. It then runs strict Ruff and
mypy checks, 90% branch coverage, isolated wheel/sdist smoke tests, and the full
tox matrix for Python 3.8 through 3.14 plus the Python 3.8 minimum-dependency
environment. The minimum environment must also pass `pip check`. Do not commit
unless all environments pass; if an interpreter or dependency is missing,
install it or explicitly report the blocker instead of skipping that gate.

The artifact gate copies the project into a temporary source tree outside the
checkout, builds one wheel and one sdist, validates their metadata and wheel
contents, and installs each artifact into its own temporary environment. It
imports every public module and exercises CLI help and the SQL support matrix
from the installed package.

Strict Ruff, Ruff format, and mypy checks run over their complete configured
targets. Existing findings are tracked in a committed per-file and per-rule
baseline; every new finding or count increase fails, while debt removal passes
and is reported. Do not refresh the baseline to make a feature change pass.
Baseline updates are reserved for deliberate tooling upgrades or reviewed debt
cleanup and use the explicit
`python -m release_routines.lib.quality_debt lint --write-baseline` and
`python -m release_routines.lib.quality_debt type --write-baseline` workflows.

Do not run tests against external, shared, or production databases. Unit tests
must use fake connections, monkeypatching, and the autouse env fixture in
`tests/conftest.py`. Disposable Greenplum, Trino, and ClickHouse integration
tests are allowed only through `run_checks(area="sql", level="integration")`;
that workflow owns endpoint validation, temporary configuration, diagnostics,
and container/network/volume teardown. The `all` profile is exhaustive and
includes destructive fault groups; normal pushes run only required core and
auth jobs. On x86_64, a skipped core/auth manifest scenario is a failure.

## Fresh-Agent Sequence

1. Run `prepare_start(...)`.
2. Run focused `docs(...)` RAG retrieval.
3. Read every routed instruction file.
4. Run `workflow_status(...)`.
5. Implement only the requested coherent batch.
6. Run focused checks.
7. Run `version_bump(...)` for non-documentation changes.
8. Run `run_checks(level="precommit")`.
9. Re-run `workflow_status(...)`.
10. Commit explicit paths with `git_workflow(action="commit", ...)`.
11. Wait for every required GitHub check for the exact pushed SHA.
12. Diagnose failures, fix in-scope defects, and recommit until the new SHA is green.
13. Report the final SHA, push target, conclusions, and run URLs.

If a watch is interrupted, resume it with
`git_workflow(action="checks", sha="<exact-pushed-sha>")` or
`agent_tools/mcp_tool.sh git-workflow checks --sha <exact-pushed-sha>`.
Never use the newest run on `dev` as a proxy for that SHA. Missing authentication,
API access, required workflows, or a bounded watcher timeout is a blocker.
