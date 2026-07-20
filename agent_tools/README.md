# Agent Tools

This directory contains repository-local tools for coding agents working in this
checkout. These tools are not part of the public `analytics-toolkit` package API
and are not installed through package extras.

## MCP Interface

Use the repository-local MCP server as the normal agent interface for startup,
documentation retrieval, workflow status, version bumps, checks, git workflow,
and release workflow entrypoints. For terminal validation and planning, use the
wrapper:

```bash
agent_tools/mcp_tool.sh prepare-start --task "implementation" --module agent_tools
agent_tools/mcp_tool.sh docs "specific topic" --mode search --top-k 5
agent_tools/mcp_tool.sh workflow-status --task "implementation" --module agent_tools --instructions-read
agent_tools/mcp_tool.sh change-impact --task "change sql.read" --module sql --symbol sql.read
agent_tools/mcp_tool.sh version-bump "Updated agent workflow" --dry-run
agent_tools/mcp_tool.sh version-bump --change-type release --force-release --dry-run
agent_tools/mcp_tool.sh run-checks --area agent_tools --level focused --dry-run
agent_tools/mcp_tool.sh run-checks --area sql --level integration --dry-run
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile core
agent_tools/mcp_tool.sh git-workflow commit --message "Update agent workflow" --path agent_tools/mcp_server.py --path tests/test_agent_tools_mcp.py
agent_tools/mcp_tool.sh git-workflow checks --sha <exact-pushed-sha>
agent_tools/mcp_tool.sh release-workflow --action merge-dev
agent_tools/mcp_tool.sh release-workflow --action status
```

MCP tools return compact receipts by default. Successful summary responses omit
internal command records; failures return a structured blocker with one bounded
excerpt. Complete stdout and stderr remain available with private permissions
below `.rag_index/tool_logs/`. Use `--detail diagnostic` for command metadata
after a blocker or `--detail full` only when complete inline output is necessary.
Response telemetry reports per-section sizes, the applicable response budget,
and raw, returned, and suppressed output bytes.

Startup records an environment fingerprint and reuses a healthy `.venv` while
requirements, project metadata, tox configuration, and the Python runtime are
unchanged. Dependency installation is quiet when the fingerprint changes.

`change-impact` is a read-only preflight for implementation work. For SQL public
symbols it reports the live signature, exact integration-manifest JSON pointers,
signature drift, documentation paths, focused checks, changelog action, and SQL
module line-budget headroom before editing begins.

Use `git-workflow commit` only when the current batch is ready to commit and
push to `origin/dev`, and pass explicit `--path` values so unrelated local
changes are not staged. The commit workflow runs the dev push automatically;
use standalone `git-workflow push` only to retry a failed post-commit push. Use
`release-workflow --action merge-dev` to fast-forward `main` from `origin/dev`
before a PyPI release. Use `release-workflow --action publish` only when release
readiness is clean.

Normal `version-bump` calls add the supplied summary under `## Unreleased` and
roll the section into a new version when it reaches ten bullets. For an explicit
release below that threshold, use `--change-type release --force-release`
without a summary. Forced release is rejected for non-release change types and
when there are no unreleased entries.

`prepare-start` persists a private startup-context receipt below `.rag_index/`.
`workflow-status` reuses it to return compact branch, dirty-state, diff, check
count, and change information without repeating routing, metadata, or full check
commands. Use `--detail diagnostic` when the complete routing and check plan is
needed, and use the consolidated status instead of separate shell probes.

Docs search summary results contain only citations, headings, and bounded
snippets. Ranking scores, source metadata, and the supporting result list for
`ask` mode are available through diagnostic detail.

`run-checks --area sql --level integration` starts the repository-owned
disposable Trino/MinIO/Iceberg/Hive and ClickHouse/Keeper stack, adds Greenplum on
x86_64 hosts, runs the marked integration tests, captures failure logs, and
always removes its containers and volumes. It must not be redirected to
external database endpoints.

Pre-commit checks run as a quick gate followed by a full gate. The quick gate
covers metadata, documentation, compilation, pytest, Ruff, and mypy; the full
gate covers coverage, artifacts, and every supported Python environment. A
quick failure prevents the expensive full gate, while a successful managed run
always records both. Check failures include the machine-readable pre-commit stage, failing pytest
node IDs, quality-debt increases, architecture overages, and tox environments
when available. A monotonic coverage-floor increase is reported as
`coverage_ratchet_confirmation` with the changed targets so it can be reviewed
and the mandatory check rerun.

The integration entrypoint accepts `--integration-profile core`, `auth`, `all`,
or `fault`; `all` is the exhaustive local default and includes every fault
group. Core and auth are required on each `dev` push. Fault is destructive and
runs nightly or by manual dispatch in database, staging, and authentication
groups. Each profile writes Compose logs, service health, JUnit output,
collection output, query/object diagnostics, and leak reports below
`.integration-artifacts/<profile>/` and always tears down its project
containers, networks, and volumes. Greenplum completeness and the no-skips gate
require x86_64; ARM runs are useful but not the complete deterministic matrix.

Successful `git-workflow commit` and `push` operations automatically watch the
immutable SHA captured immediately before the push. The watcher discovers every entry in
`.github/required-workflows.json`, polls Actions jobs plus commit check-runs and
statuses, and returns run/job URLs and conclusions. It fails on cancellation,
supersession, missing workflows, terminal failures, API/authentication errors,
or timeout. Only conditional skips declared in the manifest are accepted.
When a run fails, it waits for terminal state and returns failed steps plus a
bounded `gh run view --log-failed` excerpt. Rerun only for demonstrated
infrastructure failure. When resuming after interruption, use
`git-workflow checks --sha ...`; do not inspect the latest branch run instead.
Each watch call waits for a bounded interval (60 seconds by default), persists
the repository, exact-SHA deadline, and last reported workflow/job/check states
below `.rag_index/`, and returns only changed states plus the remaining required
checks while pending. Large first-poll sets return bounded status-only samples
plus total counts. Terminal success returns required conclusions and URLs;
diagnostic/full detail retains the expanded evidence. Successful polling API
payloads and repeated repository discovery are not returned.

`mcp_server.py` exposes the consolidated tool surface:
`prepare_start`, `docs`, `workflow_status`, `change_impact`, `version_bump`,
`run_checks`, `git_workflow`, and `release_workflow`.

## Docs Assistant

`docs_assistant.py` is the implementation behind `prepare_start` RAG indexing
and `docs(...)` retrieval. It indexes `README.md` plus Markdown files under
`docs/`, `agent_docs/`, and `agent_tools/README.md` into `.rag_index/`, then
uses heading-aware chunks and lexical BM25-like scoring to return grounded
passages with citations.

RAG is intentionally agent-only in this repository. The docs assistant is
stdlib-only and must stay outside public package CLI commands and package
extras. It does not use vector stores, hosted LLM SDKs, Ollama, or embedding
models.
