# Development Agent Instructions

Read this file for implementation, testing, build, or commit work.

## Development Commands

Use `workflow_status(...)` to get a compact repository-health receipt and check
plan for the current task. Startup context is persisted locally, so repeated
status calls report changes without repeating routing and command details. Use
`run_checks(area=..., level="focused")` for focused validation and
`run_checks(level="precommit")` before every commit.

The managed pre-commit check runs four ordered stages. A fast static gate checks
metadata, minimum constraints, documentation, compileall, Ruff, and mypy. The
coverage stage is the canonical Python 3.11 test run and enforces 90% branch
coverage. Artifact smoke tests run next, followed by the Python 3.8 through 3.14
compatibility matrix and the Python 3.8 minimum-dependency environment. Python
3.11 is omitted from that matrix because coverage already exercises it. The
matrix defaults to three parallel tox workers; set `PRECOMMIT_PARALLELISM` to a
positive integer to tune local resource use. The minimum environment must also
pass `pip check`.

Each successful managed stage writes a private receipt below `.rag_index/`.
Interrupted or failed runs may reuse a stage for 24 hours only when the working
tree, stage command, toolchain versions, and parallelism are identical. Reports
distinguish executed, reused, and failed stages. Any tracked-tree or toolchain
change invalidates the affected receipt. Do not commit unless every stage
passes or has a current exact-match receipt; if an interpreter or dependency is
missing, install it or explicitly report the blocker instead of skipping that
stage.

The artifact gate copies the project into a temporary source tree outside the
checkout, builds one wheel and one sdist, validates their metadata and wheel
contents, and installs each artifact into its own temporary environment. It
imports every public module and exercises CLI help and the SQL support matrix
from the installed package. Fresh environments share the repository-local
`.tox/pip-cache` download cache to avoid unnecessary network work.

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
includes destructive fault groups and resource-intensive stress scenarios;
normal pushes run advisory core and auth jobs. Fault and stress profiles
run nightly or by manual dispatch. On x86_64, a skipped core/auth manifest
scenario is a failure.

Do not invoke local SQL integration as a normal implementation-completion step,
including when an integration scenario was added or changed. Run it only when
the user explicitly requests local integration validation or during release
readiness. Normal work ends with focused and pre-commit validation, followed by
the exact-SHA required-check watch.

## Test Layout

Tests use a module-first tree under `tests/`, mirroring production package paths
before adding function or behavior directories for larger areas. Pytest collects
all Python filenames in that tree, so test modules omit the redundant `test_`
prefix while test functions keep the standard `test_*` names. Filenames must not
repeat ancestor areas such as `sql` or `ab_utils`, and catch-all names such as
`edges`, `improvements`, `coverage`, and `round2` must be distributed to the
subsystem they exercise.

Prefer cohesive test modules below 500 lines; no Python file under `tests/` may
exceed 700 lines. Put reusable fakes, factories, path helpers, and other
non-collected support in the nearest `_support` package. Keep global fixtures in
`tests/conftest.py`, scope area fixtures to the nearest area `conftest.py`, and
derive repository paths through `tests._support.paths.REPO_ROOT` rather than
counting parents from an individual test file.

Do not wait for, poll, or extend a turn for advisory integration completion
before finishing a normal commit. During the push watch, poll required checks
only; report an advisory integration status or URL if it is already available.
If a non-green integration result is already known while planning a task,
include its correction in that plan. Every correction derived from an
integration failure must include a fast non-integration regression test using
fakes, configuration inspection, or a bounded simulation of the failure mode.

## Fresh-Agent Sequence

1. Run `prepare_start(...)`, including during planning when session rules allow
   startup preparation. Existing startup authorization persists; do not ask
   again solely because work is a plan. Follow the root read-only exception
   when the user explicitly skips sync, disclose staleness, and revalidate after
   normal startup before edits or tests. Repository policy cannot override
   higher-priority session restrictions.
2. Read `instruction_routing.read_next`; do not reread auto-discovered `AGENTS.md`.
3. Run `change_impact(...)` for consolidated focused RAG, contract, architecture,
   documentation, and check planning.
4. Run `workflow_status(...)`.
5. Implement only the requested coherent batch.
6. Run focused checks.
7. Run `version_bump(...)` for non-documentation changes.
8. Run `run_checks(level="precommit")`.
9. Re-run `workflow_status(...)`.
10. Commit explicit paths with `git_workflow(action="commit", ...)`.
11. Wait for every required GitHub check for the exact pushed SHA. Poll required
    checks only; report but do not poll or wait for advisory integration jobs.
12. Diagnose required-check failures, fix in-scope defects, and recommit until
    the new SHA's required checks are green.
13. Report the final SHA, push target, required conclusions, and advisory
    integration status or run URL when available.

Default direct `docs(...)` calls to `top_k=3`, avoid parallel broad reads, and
inspect cited line ranges with narrow `rg` queries. On failure, act on the
structured blocker before requesting diagnostic detail or reading `log_ref`.
An unchanged failure receipt means the tree should change before retry. Use
`workflow_metrics(...)` for response-cost analysis; its token count is a
serialized-byte estimate, not model billing telemetry.

If a watch is interrupted, resume it with
`git_workflow(action="checks", sha="<exact-pushed-sha>")` or
`agent_tools/mcp_tool.sh git-workflow checks --sha <exact-pushed-sha>`.
Never use the newest run on `dev` as a proxy for that SHA. Missing authentication,
API access, required workflows, or a bounded watcher timeout is a blocker.
