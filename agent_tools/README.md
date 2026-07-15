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
disposable Trino/MinIO/Iceberg and ClickHouse/Keeper stack, adds Greenplum on
x86_64 hosts, runs the marked integration tests, captures failure logs, and
always removes its containers and volumes. It must not be redirected to
external database endpoints.

The integration entrypoint accepts `--integration-profile core`, `auth`, `all`,
or `fault`; `all` is the exhaustive default. Core and auth are required on each
`dev` push. Fault is destructive and runs nightly or by manual dispatch. Each
profile writes Compose logs, service health, JUnit output, and leak reports below
`.integration-artifacts/<profile>/` and always tears down its project containers
and volumes. Greenplum completeness requires x86_64; ARM runs are useful but not
the complete deterministic matrix.

Successful `git-workflow commit` and `push` operations automatically watch the
exact pushed SHA. The watcher discovers every entry in
`.github/required-workflows.json`, polls Actions jobs plus commit check-runs and
statuses, and returns run/job URLs and conclusions. It fails on cancellation,
supersession, missing workflows, terminal failures, API/authentication errors,
or timeout. Only conditional skips declared in the manifest are accepted.
When resuming after interruption, use `git-workflow checks --sha ...`; do not
inspect the latest branch run instead.

`mcp_server.py` exposes the consolidated tool surface:
`prepare_start`, `docs`, `workflow_status`, `version_bump`, `run_checks`,
`git_workflow`, and `release_workflow`.

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
