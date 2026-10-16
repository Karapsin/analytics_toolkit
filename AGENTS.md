# AGENTS.md

## Scope

These instructions apply to the whole repository.

## Mandatory Agent MCP Tools

Coding agents in this repository must use the repository-local MCP tools for
startup context, RAG retrieval, routing, repo status, version/changelog checks,
test recommendations, checks, commits, pushes, and release workflow entrypoints.
If MCP is already available, call
`prepare_start(task, user_prompt="<exact starting user prompt>", module=None, root=".", index_dir=".rag_index",
ensure_project_env=True)` before any repository search, file inspection, tests,
or edits. `prepare_start` switches to `dev`, pulls `origin dev`, prepares the local agent
and project environment, refreshes `.rag_index/`, returns repo health, and
reports the instruction files that must be read next.

If MCP is not available, first perform read-only Git status and operation-state
inspection using the clean-start rules below. Stop if the checkout is dirty or
an operation is unfinished. Then run the mandatory `git switch dev` and
`git pull --ff-only origin dev`,
then set up the local agent-only MCP environment with Python 3.10 or newer and
call `prepare_start` before continuing:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r agent_tools/requirements-mcp.txt
```

The committed `.codex/config.toml` registers the repository MCP server for
trusted project sessions. Codex loads project MCP configuration at session
startup, so restart Codex or reopen the workspace after bootstrapping or
changing the configuration. `agent_tools/mcp_tool.sh` with no arguments starts
the stdio server; arguments continue to invoke its manual JSON CLI.

### Isolated Sessions and Plan Mode

Bare `codex` from this repository launches a separate clone, switches that clone
to `dev`, pulls `origin/dev` with `--ff-only`, prepares its environment and RAG,
then enters native Plan mode. The shared checkout is not the feature workspace.
Each clone owns its Git state and feature branch; never share a feature clone.

In launcher sessions, `prepare_start(...)` detects the private session receipt.
During planning it refreshes context without switching branches or pulling.
When resuming feature work it preserves the feature branch and unfinished work.
Before any implementation, leave Plan mode and call
`git_workflow(action="start")`. This pulls dev before creating a unique branch.
If it returns `status="revalidate"`, assess the changed paths against the approved
plan, obtain approval for material revisions, then acknowledge that receipt with
`git_workflow(action="start", sha="<after-sha>")`. No feature edits before this step.

Ordinary bootstrap/release checkouts without a session receipt still use normal
clean startup synchronization. Higher-priority session restrictions apply;
repository policy cannot authorize a pull prohibited by the active Plan mode.

### Read-Only Planning Exception

Agents may skip `git switch dev`,
`git pull --ff-only origin dev`, and
`prepare_start(...)` only when the user explicitly authorizes skipping startup
sync. When using this exception, state that findings may be stale because the
pull was skipped. Do not edit files, run mutating workflows, run tests, commit,
push, publish, or otherwise change repository state while relying on the
exception.

Before leaving this read-only exception for edits, tests, commits, pushes, or
release actions, run the normal `prepare_start(...)` workflow and re-check the
files or areas covered by the plan. If pulled changes conflict with, invalidate,
or materially change the plan, stop before editing and explain what changed, why
it blocks or alters the plan, and suggest concrete next options. If pulled
changes do not affect the plan, continue normally and mention that the plan was
revalidated after startup sync.

Use these MCP tools for the corresponding agent workflow steps:

- Startup orchestration and required instruction routing: `prepare_start`.
- Original user task recording: `prepare_start(user_prompt=...)`; preserve the
  exact message, not an agent summary. Append later task corrections with
  `task_context(action="append", user_prompt=...)`. Private originals live under
  `.git/atk-tasks/`; the PR workflow attaches redacted context automatically and
  binds its digest to the signed head. Launcher-only preparation may omit the prompt.
- Record assistant clarification questions, including every offered option, with
  `task_context(action="question", evidence={"text": "<exact question>"})` before
  asking. Bind the actual response with `answer-question`, its returned
  `question_id`, and the exact `user_prompt`; use `correction_index` when that
  response is already recorded. Never record assistant text as a user message.
  Record the exact proposed plan with `plan`, then bind actual implementation
  approval with `approve-plan`. Use the wrapper's `--evidence-file` option when
  the active MCP server has not yet reloaded the extended interface.
- RAG retrieval: `docs(query, mode="search"|"ask", top_k=3)`.
- Consolidated implementation preflight: `change_impact`.
- Per-startup response and retry accounting: `workflow_metrics`.
- Repository routing, health, metadata, and recommended checks:
  `workflow_status`.
- Version, README version, and changelog updates: `version_bump`.
- Focused, pre-commit, and release validation checks: `run_checks`.
- Headless current-host SQL Explorer capture and review: `visual_workflow` and
  `visual_review`.
- Stage/commit and push workflow: `git_workflow`.
- Release readiness and PyPI publishing entrypoint: `release_workflow`.

Use the default compact `summary` responses for normal workflow progress. Request
`detail="diagnostic"` only after a blocker needs bounded evidence, and
`detail="full"` only when the persisted `log_ref` cannot answer the question.
Successful internal command output is intentionally omitted from summary and
diagnostic responses; inspect the referenced private log selectively instead of
returning an entire build or test transcript to the model.

For implementation work, use `change_impact(...)` after startup routing instead
of duplicating separate RAG, contract, architecture, documentation, and test-plan
lookups. Do not batch multiple potentially large retrievals or broad file reads
into one response; follow citations with narrow searches and line ranges. Act on
structured check blockers first, and do not rerun an unchanged failure without
changing the tree.

Normal feature work uses the session-owned branch and a PR to `dev`.
Run local focused and mandatory pre-commit checks, then
`git_workflow(action="commit", message="...", paths=[...])`. It stages explicit
paths, commits, pushes the signed feature branch, verifies its task attachment,
and hands the published PR to the Pi. Local agents finish with the PR link;
they do not wait for review, approval, GitHub checks or merge and do not correct
handed-off branches. `git_workflow(action="handoff")` recovers an interrupted
handoff. Publication failures retain local ownership.
The Pi owns conflicts, fast CI, automatic fresh correction agents and independent
review until approved. Rejections describe changes and reasons in plain Telegram
alerts without approval buttons. Genuine unanswered questions, owner veto/cancel,
provenance failures and subscription/authentication limits pause the appropriate
work; routine reviewer rejection does not require an owner resume command.
Handoff archives private task context and refreshes clean local dev immediately;
`git_workflow(action="sync")` retries pending synchronization without waiting for
merge. Shared dev updates under a lock only when clean and already on dev. Report
pending synchronization without discarding work. Reenter native Plan mode before
another task, using a fresh feature/task ID and changelog fragment.
`git_workflow(action="feedback", pr=<number>)` is optional read-only status.
Merged branch cleanup preserves active worktrees, unpublished work, dev and main;
unknown and squash-only unique local branches are reported and preserved.

Local agents do not investigate or wait for advisory integration. The GitHub
agent queues integration on each final reviewed PR head before merge, reruns
changed heads after conflicts or repairs, and owns post-merge monitoring through
completion and repair PRs until green. Routine push and nightly integration
triggers are disabled; manual release validation remains available.
Integration success does not gate dev merges. Its review/conflict/repair jobs
use trusted default-branch controller prompts rather than local-session startup
or MCP branch mutations. Controller jobs never execute PR code with write
credentials. Repair PRs need independent review, fast CI, and fast non-integration
regression coverage. SQL Explorer fixes retain the visual-review requirement.
The private host never runs tests or renders scenes. Airflow schedules the host
worker through its private socket, using ChatGPT subscription authentication.
For visual repairs only, the controller may publish an isolated temporary
`agent-visual/<sha>` candidate to obtain GitHub headless captures. Every full PNG
is reviewed individually before publishing the correction to a feature PR;
the receipt binds the capture, task and patch, and the temporary ref is removed.

The initial automation bootstrap may use the legacy dev commit workflow before
PR protection is activated. That workflow watches all required checks for its
exact pushed SHA. `main` remains the default and release branch; only the
explicitly authorized controller/workflow automation allowlist may be synced
there outside a release. Package code reaches main only through releases.

Any change under `analytics_toolkit/sql_explorer/`, its visual harness, or its
scene manifest requires the full SQL Explorer visual review before commit or
push. Use `visual_workflow` to capture the application headlessly on the current
Linux, macOS, or Windows host. No VM, OS image download, desktop session, or VNC
server is required. Render every manifest scene with Textual's headless driver
at a fixed 208x47 terminal grid and export full 1280x800 PNGs. Open every full
PNG and record an individual `pass` verdict with `visual_review`. Completion
requires every automated geometry check and agent verdict to pass, and removes
only the run-owned temporary checkout. The receipt records the host and binds
to the full reviewed content. `git_workflow` blocks SQL Explorer commits and
pushes when this receipt is missing, stale, partial, or non-green. Treat reference images as design guidance, not pixel baselines.

Local advisory integration investigation requires an explicit user request or release
readiness scope. An incidentally observed failure does not expand the current
task: record a deferred follow-up using only the evidence already available.
This boundary takes precedence over general instructions to investigate or
correct known failures. Do not fetch additional evidence to describe a follow-up.
Whenever an integration failure leads to a code, configuration, test-harness,
or workflow fix, add or strengthen a non-integration regression test that can
detect the same failure mode quickly without starting external services.

Use `workflow_status(...)` before and after repository changes. Use
`version_bump(...)`, `run_checks(...)`, `git_workflow(...)`, and
`release_workflow(...)` for the mandatory repository workflows instead of
calling the underlying scripts directly, except for the initial bootstrap path
described above.

For terminal/manual validation of these same agent MCP tool functions, use the
repository wrapper instead of inline Python:

```bash
agent_tools/mcp_tool.sh prepare-start --task "implementation" --module agent_tools
agent_tools/mcp_tool.sh docs "specific topic" --mode search --top-k 3
agent_tools/mcp_tool.sh docs "specific question" --mode ask
agent_tools/mcp_tool.sh change-impact --task "specific change" --module sql --symbol sql.read
agent_tools/mcp_tool.sh workflow-metrics
agent_tools/mcp_tool.sh workflow-status --task "documentation" --module sql
agent_tools/mcp_tool.sh version-bump "Updated SQL docs" --dry-run
agent_tools/mcp_tool.sh run-checks --area sql --level focused --dry-run
agent_tools/mcp_tool.sh visual-workflow start
agent_tools/mcp_tool.sh visual-workflow capture --review-id <review-id>
agent_tools/mcp_tool.sh visual-review --review-id <review-id> --scene-id <scene-id> --verdict pass
agent_tools/mcp_tool.sh visual-workflow complete --review-id <review-id>
agent_tools/mcp_tool.sh release-workflow --action merge-dev
agent_tools/mcp_tool.sh release-workflow --action status
```

Use `git-workflow commit` only when the current batch is ready to commit and
push its session-owned feature branch. Use standalone `git-workflow push` only to retry a failed
post-commit push. Use `release-workflow --action publish` only when release
readiness is clean.

If MCP setup or `prepare_start` fails because of local changes, merge
conflicts, authentication, network issues, dependency installation failure,
divergent history, or another startup blocker, stop and report the structured
blocker instead of continuing. MCP tools do not replace required instruction
reading or approval rules. MCP release and git workflow tools must still honor
repository safety rules and must not access databases or read `.connections`.

## Project Overview

`analytics_toolkit` is a Python `>=3.8,<3.15` utility package with six public
areas:

- `analytics_toolkit.ab_utils`: AB-test metric comparison helpers.
- `analytics_toolkit.sql`: SQL read/execute/load/transfer helpers for Greenplum, Trino, and ClickHouse.
- `analytics_toolkit.excel`: long-format dataframe to Excel report helpers.
- `analytics_toolkit.dates`: date and period helpers.
- `analytics_toolkit.general`: shared logging and file path helpers.
- `analytics_toolkit.datalens_utils`: configuration-driven DataLens dashboards.

Keep public APIs stable unless the user explicitly asks for a breaking change.
Many tests import underscore helpers through package re-export modules, so treat
exported internals as compatibility surface too.

## Toolkit Helpers In Consumer Code

When writing scripts, notebooks, usage examples, or reporting code that consumes
this package, use matching public `analytics_toolkit` helpers before implementing
equivalent pandas or stdlib utilities. Read the
[analytics-toolkit skill](.agents/skills/analytics-toolkit/SKILL.md) for helper
selection, public imports, and date/timestamp semantics. Explicit user choices
and requirements outside the helper contracts take precedence. Prefer
`from atk import *` where appropriate and use its bare aliases such as `dt`,
`dttm`, `sql`, and `ab`. Internal code implementing the toolkit's helpers follows
module contracts and may use the underlying pandas or stdlib primitives.

## Required Context Routing

Root `AGENTS.md` is the auto-discovered instruction file. The files under
`agent_docs/` are not auto-loaded unless this file routes you to them.

After `prepare_start(...)` and focused `docs(...)` retrieval, read the relevant
files before normal repository inspection, tests, or edits:

- Any implementation, testing, build, or commit work: `agent_docs/development.md`.
- Public documentation work under `docs/` or README documentation sections: `agent_docs/documentation.md`.
- PyPI publishing, package releases, or release workflow changes: `agent_docs/release.md`.
- SQL module work: `agent_docs/sql.md`.
- AB utilities work: `agent_docs/ab_utils.md`.
- Excel helper work: `agent_docs/excel.md`.
- Date helper work: `agent_docs/dates.md`.
- General helper work: `agent_docs/general.md`.
- DataLens module work: `agent_docs/datalens_utils.md`.
- Instruction maintenance for this file or `agent_docs/`: read this file and the specific instruction files being edited.

If multiple categories apply, read all relevant files before editing. Keep
retrieved and opened context focused on the task.

For SQL consumer code, prefer `from atk import *` when appropriate and use its
`sql` alias. When explicit imports are needed, use
`from analytics_toolkit import sql` or `import analytics_toolkit.sql as sql`.
Do not restore removed root implementation paths.

## Agent-Only RAG Context Workflow

For any repository-related work, use the local docs RAG workflow before normal
repository search or file inspection. This includes implementation, reviews,
documentation edits, usage examples, API explanations, behavior investigations,
and answers about project conventions. Skip RAG only for clearly non-repository
requests, such as simple shell/time/status commands unrelated to project
behavior.

RAG is intentionally an agent-only repository workflow, not a public
`analytics-toolkit` package feature. Keep docs retrieval tooling under
`agent_tools/`, keep it runnable from a checkout, and do not add public CLI
commands, package extras, vector-store dependencies, hosted LLM SDKs, Ollama,
or embedding-model dependencies for it.

Use `docs(query, mode="search", top_k=3)` for ranked snippets and
`docs(query, mode="ask")` for a grounded no-LLM summary with citations. Keep
retrieved context focused; rebuilding `.rag_index/` is local work and does not
itself consume LLM context tokens, but reading retrieved output does.

Treat normal repository search, file inspection, and tests as secondary context
after the RAG pass, not as substitutes for it. If RAG is unavailable, blocked,
or returns no useful context after rebuilding, explicitly report that fallback
was needed, then use normal repository search and file inspection. When fallback
was needed because docs were missing or unclear, finish by proposing the
specific documentation update that would make future RAG retrieval unambiguous.

## Global Rules

- Automated PR handling is restricted to the repository owner's enrolled-machine
  signatures and trusted App repairs. Every review must use a fresh read-only
  agent session separate from every candidate author; verdicts bind to the head,
  dev base and user-task digest. Misalignment, ambiguity and quality blockers
  reject the candidate and start fresh Pi correction and independent review.
  Ambiguity requiring actual user input pauses for authenticated Telegram answers.
- Owner exact-head `/agent approve-policy <sha>` approval is retained for
  `.github/agent/`, `.codex/`, the controller/dispatch/capture/publishing workflows,
  `.github/required-workflows.json` and `agent_tools/machine_signing.py`.
  Ordinary instructions, tools, integration repairs and test workflows use
  independent prompt-aligned review and fast CI. Both renamed paths are checked.
  Local agents must not post owner approval. The authenticated Telegram handler
  may post a transparent App-authored approval following the pinned owner's
  button click; private controller records verify that action outside the model.
  Approval alerts explain what changed, the protected boundary, why approval is
  required, the PR link and exact command. New heads require new decisions.

- Prefer small, local changes that follow existing module patterns.
- Do not alter packaging metadata or rewrite README/manual docs unless the task requires it.
- After every non-documentation repository change, use `version_bump(...)` to record
  a uniquely owned feature fragment or an entry under `## Unreleased`.
  Ordinary changes and serialized fragment folding never change the package or
  README version, regardless of the number of entries. Documentation-only work
  retains its no-bump behavior.
- An explicit PyPI release prepares one new version through a signed, independently
  reviewed PR to `dev`, before validation or TestPyPI. Use
  `release_workflow(action="merge-dev", summary=None)` and re-enter the same
  action after the preparation PR merges. Supply a release summary when there
  are no unreleased entries. Never commit release metadata directly to dev.
  Status does not prepare versions. Failed publication retries reuse the same
  reviewed candidate, version, and retained verified artifacts. Changed candidate
  content invalidates validation and requires another preparation PR; changed
  published artifacts require a new version. Keep historical release sections.
- Versions retain four parts `a.b.c.d`, each from 0 through 19. Increment once
  for release preparation, carrying at 19: `1.3.6.19` -> `1.3.7.0`,
  `1.3.19.19` -> `1.4.0.0`, `1.19.19.19` -> `2.0.0.0`.
- Isolated private controller coding workers edit source, tests, and documentation
  only. They execute no programs, tests, installs, builds, services, integration,
  or scenes; GitHub performs all validation after publication. Their task prompt
  overrides interactive startup/check/commit instructions.

- When changing dependency declarations in `pyproject.toml`, update the CRAN-style `Depends`, `Imports`, and `Suggests` dependency entries in `README.md`.
- When changing public behavior, update the relevant module README and focused tests.
- Do not run tests against external, shared, or production databases. Unit tests
  must use fake connections, monkeypatching, and the autouse env fixture in
  `tests/conftest.py`. The only real-database exception is the repository-owned,
  disposable SQL integration stack invoked through
  `run_checks(area="sql", level="integration")`. That workflow may connect only
  to the loopback/container endpoints it creates, must generate `.connections`
  in a temporary directory, and must always tear down containers and volumes.
- Keep `.connections` out of the repo. Tests should create a temporary `.connections` and chdir into that temp project.
- Use existing structured parsers for SQL/table names (`sqlparse`, `sqlglot`) instead of ad hoc parsing where those modules already do the job.
- At the end of every non-documentation change, run `run_checks(level="precommit")` before committing, even if focused tests were run earlier. For documentation-only changes, full checks are not required; run focused tests only when the documentation change affects tested paths or generated artifacts. Treat test failures and pytest warnings as issues to fix before finishing; the final test run should pass with no warning summary.
- Managed pre-commit validation runs ordered static, coverage, artifact, and Python-version matrix stages. Coverage is the canonical Python 3.11 test run; a stage failure stops downstream work, and only exact-tree, exact-toolchain successful-stage receipts may be reused. The final successful run must pass every stage.
- SQL integration `all` is the exhaustive local profile and includes the
  destructive database, staging, and authentication fault groups. The GitHub
  agent queues advisory candidate validation before merge with zero skipped
  manifest scenarios on x86_64; profiles remain available by manual dispatch.
  Integration completion is
  mandatory only during release readiness, which must pass the exhaustive `all`
  profile with both ClickHouse transports. Integration cleanup must remove
  project containers, networks, volumes, labelled queries, tables, and MinIO objects.
- For normal implementation, test, documentation, commit, and push tasks, do not
  invoke `run_checks(area="sql", level="integration")`. Adding or changing an
  integration scenario does not make a local integration run mandatory. Invoke
  it only when the user explicitly requests local integration validation or the
  task is release readiness; otherwise add the scenario and its fast
  non-integration regression coverage, run focused and pre-commit checks, and
  let the advisory push workflows execute without waiting for them.
- Once a coherent batch of changes is done, run `git_workflow(action="commit", message="...")`, replacing `...` with a short description of the changes.

Telegram feature requests require separate validity, read-only planning, independent
plan review and coding sessions. Questions pause work until authenticated owner
answers arrive. The exact prompt, responses, plan and dev SHA bind the approval.
No coding before that approval; every resulting PR gets another separate reviewer.
