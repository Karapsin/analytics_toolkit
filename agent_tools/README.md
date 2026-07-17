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

`prepare-start`, `run-checks`, and `git-workflow` return compact command records
by default. Each record contains status, duration, a short summary, and a local
`log_ref`; complete stdout and stderr are stored with private permissions below
`.rag_index/tool_logs/`. Use `--detail diagnostic` for bounded output excerpts or
`--detail full` only when complete inline output is necessary. Response telemetry
reports raw, returned, and suppressed output bytes so response-size regressions
are visible.

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

`workflow-status` reports the current branch, dirty state, `git status --short`
lines, unstaged `git diff --stat` lines, and staged `git diff --cached --stat`
lines through its `repo_health` result. Use that consolidated status instead of
running separate shell probes for routine worktree summaries.

`run-checks --area sql --level integration` starts the repository-owned
disposable Trino/MinIO/Iceberg/Hive and ClickHouse/Keeper stack, adds Greenplum on
x86_64 hosts, runs the marked integration tests, captures failure logs, and
always removes its containers and volumes. It must not be redirected to
external database endpoints.

Check failures include the machine-readable pre-commit stage, failing pytest
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
the exact-SHA deadline and last reported workflow states below `.rag_index/`, and
returns `status: pending` plus the immutable SHA when another call is required.
Successful polling API payloads are parsed and discarded rather than returned.

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
