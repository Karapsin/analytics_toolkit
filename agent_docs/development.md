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
coverage, plus the stricter committed statement and branch ratchets in
`release_routines/coverage_targets.json`. Artifact smoke tests run next, followed
by the Python 3.8 through 3.14 compatibility matrix and the Python 3.8
minimum-dependency environment. Python
3.11 is omitted from that matrix because coverage already exercises it. The matrix
also runs offline DataLens conformance on Python 3.10 with SDK 3.1.0 and 3.2.0.
The matrix defaults to three parallel tox workers; set `PRECOMMIT_PARALLELISM` to a
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
only; report an advisory integration status or URL only if returned incidentally
by that watch. Follow the authoritative advisory policy in `AGENTS.md`: do not
query advisory jobs, download logs or artifacts, diagnose, retry, or repair
them, even while required checks run. Explicit user investigation scope or
release readiness is required. Record known advisory failures as deferred
follow-ups using existing evidence; they do not expand the current task.
Every correction derived from an
integration failure must include a fast non-integration regression test using
fakes, configuration inspection, or a bounded simulation of the failure mode.

## Fresh-Agent Sequence

1. Launch bare `codex` from the canonical repository. The launcher prepares a
   separate clone on refreshed dev before entering native Plan mode.
2. Call `prepare_start(...)` for role-aware context and read its routed files.
   Planning and feature resume do not switch or pull branches.
3. Plan the task. After leaving Plan mode, call `git_workflow(action="start")`.
   Revalidate against any incoming dev changes; acknowledge a revalidation
   receipt with `sha=<after-sha>` after approving material plan revisions.
4. Run `change_impact(...)` and `workflow_status(...)`, then implement on the
   session-owned feature branch.
5. Run focused checks and `version_bump(...)` (creates a unique fragment).
6. Run mandatory `run_checks(level="precommit")`, then workflow status.
7. Commit explicit paths with `git_workflow(action="commit", ...)`, which pushes
   the feature branch and opens its PR to dev.
8. Wait for agent review and fast CI with `git_workflow(action="feedback")`.
   Respect the GitHub writer lease. Refresh after bot commits before corrections.
9. Fix feedback and repeat checks/commits until merged; do not monitor integration.
10. Call `git_workflow(action="sync")`; report pending shared sync safely.
11. Reenter native Plan mode before starting another feature.

The GitHub integration-repair role is explicitly authorized to monitor and fix
advisory integration after merge. Each repair requires fast regression coverage,
independent review, and fast CI. Integration never gates a dev feature merge.
Bootstrap-only legacy pushes retain the exact-SHA required-check watch.

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
