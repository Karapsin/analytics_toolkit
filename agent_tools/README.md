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
agent_tools/mcp_tool.sh docs "specific topic" --mode search --top-k 3
agent_tools/mcp_tool.sh workflow-status --task "implementation" --module agent_tools --instructions-read
agent_tools/mcp_tool.sh workflow-metrics
agent_tools/mcp_tool.sh change-impact --task "change sql.read" --module sql --symbol sql.read
agent_tools/mcp_tool.sh version-bump "Updated agent workflow" --dry-run
agent_tools/mcp_tool.sh version-bump --change-type release --force-release --dry-run
agent_tools/mcp_tool.sh run-checks --area agent_tools --level focused --dry-run
agent_tools/mcp_tool.sh run-checks --area sql --level integration --dry-run
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile core
agent_tools/mcp_tool.sh visual-workflow start
agent_tools/mcp_tool.sh visual-workflow capture --review-id <review-id>
agent_tools/mcp_tool.sh visual-workflow status --review-id <review-id>
agent_tools/mcp_tool.sh visual-review --review-id <review-id> --scene-id <scene-id> --verdict pass
agent_tools/mcp_tool.sh visual-workflow complete --review-id <review-id>
agent_tools/mcp_tool.sh git-workflow commit --message "Update agent workflow" --path agent_tools/mcp_server.py --path tests/agent_tools/mcp
agent_tools/mcp_tool.sh git-workflow checks --sha <exact-pushed-sha>
agent_tools/mcp_tool.sh release-workflow --action merge-dev
agent_tools/mcp_tool.sh release-workflow --action status
```

The agent-only MCP runtime requires Python 3.10 or newer and MCP SDK 2. The
committed `.codex/config.toml` registers the server automatically for trusted
project sessions. Bootstrap or refresh `.venv` with `requirements-mcp.txt`, then
restart Codex or reopen the workspace after cloning or changing MCP
configuration; MCP servers are loaded when a session starts. Running
`agent_tools/mcp_tool.sh` without arguments starts the stdio server, while the
commands above retain the manual JSON CLI.

The project registration is optional (`required = false`) so a missing local
environment does not prevent Codex from opening. The launcher never installs
dependencies automatically; bootstrap failures report the command needed to
repair the environment.

MCP tools return compact receipts by default. Successful summary responses omit
internal command records; failures return a structured blocker with one bounded
excerpt. Complete stdout and stderr remain available with private permissions
below `.rag_index/tool_logs/`. Use `--detail diagnostic` for command metadata
after a blocker or `--detail full` only when complete inline output is necessary.
Summary budgets are enforced before receipts are returned. Summary telemetry
contains final size, budget, and truncation state; diagnostic telemetry adds
per-section and raw/suppressed sizes. `workflow-metrics` aggregates the active
startup session and labels its byte-based token estimate because model billing
tokens are unavailable to repository tools.

Startup records an environment fingerprint and reuses a healthy `.venv` while
requirements, project metadata, tox configuration, and the Python runtime are
unchanged. Dependency installation is quiet when the fingerprint changes.

Bare `codex` from the canonical repository creates a private clone on `dev`,
pulls before launch, prepares the repository MCP, and selects native Plan mode
before accepting task input. Install this launcher with
`python -m agent_tools.install_sessions` from the prepared project environment.
Other directories and Codex commands continue to use the existing CLI.

Automatic handling accepts only the repository owner's PRs whose heads are SSH
signed by an enrolled machine, plus this App's signed repair commits. The launcher
creates a device key outside clones; its public key must be enrolled in the
reviewer host's private allowed-signers file. An unknown or revoked key fails
closed, independently of the model verdict.

Instruction, skill, agent-tool, release-tool, and GitHub workflow changes also
require an owner comment `/agent approve-policy <current-head-sha>`. Approval
expires when the head changes. Local agents must never write this owner approval
comment themselves. PR files cannot enroll signing keys or change this gate.

After planning, use `git_workflow(action="start")` to pull dev again before a
unique feature branch is created. A changed base requires plan reassessment;
acknowledge only after reviewing the changed plan. Independent clones prevent
concurrent sessions from sharing an index, branch, or working tree.

Feature sessions commit focused and pre-commit checked changes to their own
branch and open a PR to dev. Use `git_workflow(action="feedback")` until reviewed
and merged, `refresh` before corrections when the bot has written commits, and
`sync` after merge to refresh the session and clean canonical dev checkout.
A writer lease prevents simultaneous bot/local branch edits. Dirty canonical
work is preserved and reported as pending synchronization.

The private subscription worker is maintained in self_hosting's
`self_host/services/github_agent`. Airflow calls its Unix socket every minute
and updates the underlying Codex executable Sunday at 04:00 Europe/Moscow.
The Pi performs bounded source operations only; GitHub runs tests and renders
visual scenes. Up to five jobs run concurrently with serialized merge decisions.
Fast checks and the App's exact-head/base review gate merges. Integration runs
are monitored after merge and repaired through reviewed PRs until matching
scenario groups and transports are green. No OpenAI API key is used.

`change-impact` is a read-only preflight for implementation work. For SQL public
symbols it reports the live signature, exact integration-manifest JSON pointers,
signature drift, documentation paths, focused checks, changelog action, and SQL
module line-budget headroom before editing begins.

Use `git-workflow commit` only for a coherent checked batch with explicit paths.
In a feature session it pushes the feature and opens a PR; the current checkout's
legacy dev route remains available for bootstrap. `release-workflow` retains
its explicit main-only release process.

Only an explicit user waiver for the current batch permits
`git-workflow commit --validation-waiver "user-authorized reason"`. This records
the reason in the commit without creating a successful validation receipt; it
does not bypass path safety, signatures, writer leases, or GitHub merge gates.

SQL Explorer production or visual-harness changes also require the private
visual receipt created by `visual-workflow`. The capture runs headlessly on the
current Linux, macOS, or Windows host, using the checkout's Python environment.
It starts each scene in a separate process with Textual's headless driver and
fake database fixtures, exports the complete application screen, and rasterizes
it to a 1280x800 PNG with `resvg-py`. The terminal grid is 208x47 cells. A host
monospace font must be installed (for example Menlo, Consolas, or Liberation
Mono). No VM, OS image download, desktop session, or VNC server is needed.

Install `agent_tools/requirements-mcp.txt` in the agent environment for the PNG
renderer. On Windows, the workflow finds `.venv/Scripts/python.exe`; on Linux
and macOS it uses `.venv/bin/python`, falling back to the running interpreter.
The shell-free CLI equivalent on every platform is:

```text
python agent_tools/mcp_server.py call visual-workflow start
python agent_tools/mcp_server.py call visual-workflow capture --review-id <review-id>
```

Capture copies only reviewable checkout files to a temporary workspace and
removes that workspace afterward. Screenshots contain the application's UI,
not native window decorations. `visual-workflow status` returns the next PNG
batch; the agent must open every full image and record a verdict through
`visual-review`. Geometry checks and every agent verdict must pass before
completion. The receipt records the host and capture method and is bound to
the complete reviewed content; only a current all-pass receipt allows commit
or push. Failed or interrupted captures require a new review session.

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

Docs search defaults to three deduplicated citations with bounded snippets and
returned/considered counts. Ranking scores, source metadata, and the supporting result list for
`ask` mode are available through diagnostic detail.

`run-checks --area sql --level integration` starts the repository-owned
disposable Trino/MinIO/Iceberg/Hive and ClickHouse/Keeper stack, adds Greenplum on
x86_64 hosts, runs the marked integration tests, captures failure logs, and
always removes its containers and volumes. It must not be redirected to
external database endpoints.

Managed pre-commit checks run as separate static, coverage, artifact, and Python
matrix commands, stopping before downstream work when a stage fails. Coverage
is the canonical Python 3.11 test run; the remaining Python 3.8 through 3.14
matrix omits that duplicate and defaults to three parallel tox workers. Set
`PRECOMMIT_PARALLELISM` to a positive integer to tune it. Artifact installs use
fresh virtual environments with a shared `.tox/pip-cache` download cache.

Successful stages receive private 24-hour receipts keyed by the exact working
tree, command definition, toolchain versions, and parallelism. An interrupted
or corrected run reuses only exact matches and reports each stage as executed,
reused, or failed. Managed coverage target increases are reported and accepted
in the same coverage run, while the manual coverage command keeps its
review-and-rerun behavior. The compatibility `--quick` script mode retains its
plain pytest run, but managed pre-commit validation does not duplicate it.
Check failures include every failed stage, failing pytest
node IDs, quality-debt increases, architecture overages, and tox environments
when available. Repeated identical failures return a compact unchanged receipt
pointing to persisted evidence.

The integration entrypoint accepts `--integration-profile core`, `auth`, `all`,
`fault`, or `stress`; `all` is the exhaustive local default. Each selected
profile runs the same collection with ClickHouse HTTP and native transports.
Use `--integration-clickhouse-driver http` or `native` only for focused
diagnosis; `both` is the managed default and enforces collection parity. Core
and auth run as advisory checks on each `dev` push. Fault and stress run nightly or by
manual dispatch. Each profile writes Compose logs, service health, JUnit output,
collection output, query/object diagnostics, and leak reports below
`.integration-artifacts/<profile>/<transport>/` and always tears down its project
containers, networks, and volumes. Greenplum completeness and the no-skips gate
require x86_64; ARM runs are useful but not the complete deterministic matrix.

Successful `git-workflow commit` and `push` operations automatically watch the
immutable SHA captured immediately before the push. The watcher discovers every
entry in `.github/required-workflows.json`, waits only for `required_push`
workflows, and reports `advisory_push` workflow status without blocking. It polls
Actions jobs only for required workflows, plus shared commit check-runs and
statuses, and returns run/job URLs and
conclusions. It fails when a required check is cancelled, superseded, missing,
failed, or timed out, or when API/authentication fails. Only conditional skips
declared in the manifest are accepted.
When a required run fails, it waits for terminal state and returns failed steps plus a
bounded `gh run view --log-failed` excerpt. Rerun only for demonstrated
infrastructure failure. When resuming after interruption, use
`git-workflow checks --sha ...`; do not inspect the latest branch run instead.
Advisory status returned incidentally is report-only. Do not query advisory jobs,
download their logs or artifacts, diagnose, retry, or repair them during normal
completion, even while required checks run. Record a deferred follow-up using
existing evidence. Investigation requires explicit user scope or release
readiness; observing a failure alone is not authorization.
Each watch call waits for a bounded interval (60 seconds by default), persists
the repository, exact-SHA deadline, and last reported workflow/job/check states
below `.rag_index/`, and returns changed states plus remaining required checks
on the first pending receipt. Unchanged polls omit the repeated check list.
Large first-poll sets return bounded status-only samples
plus total counts. Terminal success returns required conclusions and URLs;
diagnostic/full detail retains the expanded evidence. Successful polling API
payloads and repeated repository discovery are not returned.

`mcp_server.py` exposes the consolidated tool surface:
`prepare_start`, `docs`, `workflow_status`, `workflow_metrics`, `change_impact`, `version_bump`,
`run_checks`, `visual_workflow`, `visual_review`, `git_workflow`, and
`release_workflow`.

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
