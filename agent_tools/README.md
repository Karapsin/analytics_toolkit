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
environment does not prevent Codex from opening. The MCP stdio entrypoint does not install dependencies automatically; bootstrap
failures report the command needed to repair the environment. The interactive
`codex` launcher prepares its isolated session before opening the CLI.

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
unchanged. Interactive sessions share a private dependency wheelhouse under
`~/.local/share/analytics-toolkit-agent/wheels/`, keyed by repository, Python/ABI,
platform, requirements, project metadata, tox configuration, and preparation profile.
Each clone owns its `.venv`; the toolkit is installed editable from that clone.
A warm cache installs without PyPI, including editable build dependencies. Versions
remain fixed until inputs change or an explicit cache refresh is requested.

Cache population is serialized and published only after offline installation,
`pip check`, and import verification. Incomplete or corrupted caches are rebuilt;
a failed refresh preserves the previous healthy generation. Downloads default to
a 120-second socket timeout and five connection retries, respecting `PIP_*`
overrides. Streaming network errors retry the whole download at most three times
with 2/5-second backoff, within a shared 15-minute deadline. Dependency conflicts
and certificate/authentication failures stop immediately. Progress goes to stderr;
full output and preparation receipts remain private below `.rag_index/bootstrap/`.
`ATK_BOOTSTRAP_CACHE_DIR` can override the wheelhouse location.

Bare `codex` from the canonical repository creates a private clone on `dev`,
pulls before launch, prepares the repository MCP, and selects native Plan mode
before accepting task input. Install this launcher with
`python -m agent_tools.install_sessions` from the prepared project environment.
After updating the launcher, rerun the installer and source
`~/.local/share/analytics-toolkit-agent/codex.zsh` in existing terminals.
Other directories and Codex commands continue to use the existing CLI.

From the canonical checkout, `codex --resume-bootstrap <session-id>` continues a
failed preparation in its original clone. It accepts only a clean `dev` clone in
`launching` phase with no unfinished Git operation. It refreshes dev before loading
the clone's preparation tools, and concurrent attempts are rejected. A failed
preparation prints its session ID, receipt location, and resume command; it never
enters Plan mode or deletes the clone. `codex --refresh-bootstrap-cache` creates a
new isolated session and refreshes dependency versions even when inputs match.
The first preparation requires PyPI; Git synchronization still requires its remote.

Automatic handling accepts only the repository owner's PRs whose heads are SSH
signed by an enrolled machine, plus this App's signed repair commits. The launcher
creates a device key outside clones; its public key must be enrolled in the
reviewer host's private allowed-signers file. An unknown or revoked key fails
closed, independently of the model verdict.

`prepare_start(..., user_prompt="exact starting message")` records the original
task once under private `.git/atk-tasks/`. Append task corrections with
`task_context(action="append", user_prompt="exact correction")`. Retries and
resumes preserve the original; verified PR handoff archives it before another task. CLI calls
accept `--user-prompt-file` so prompt text does not need shell interpolation.
The PR workflow attaches redacted context and a signed-commit digest. Private
originals never appear in tool receipts. MCP-supplied text relies on the enrolled
machine trust boundary; it is not independent proof of human authorship.

Oversized attachments retain the complete task in a content-addressed Git archive
instead of truncating messages or repeating hidden payloads in the PR body.
The compact attachment links to readable history pages containing the original
prompt, chronological corrections, agent questions and their complete user
answers, plans, decisions and image records. Bounded canonical JSON chunks retain
the exact signed task bytes. Readers verify chunk sizes and SHA-256 hashes,
canonical task identity, the immutable archive layout and the candidate's existing
signed trailers. Legacy inline v1/v2 attachments remain readable.

Archive refs use `refs/tags/agent-task-evidence/v1/<task-digest>` and contain only
task data, outside the candidate tree and its visual receipt. Archives grant no
approval and do not promote package code. Publication preflights the complete PR
body and all archive bounds before mutations, confirms the immutable archive
before the feature push, and rechecks current authority after archive I/O.
An interrupted archive create is recovered by its exact identity; conflicting
refs are never replaced. Keep archive refs for the task's audit history.
The v1 archive layout is a frozen transport contract covered by a historical
fixture; a future layout needs a new version and a retained v1 reader.

Controller code/prompts, security configuration, dispatch/capture/publishing
workflows and the required-check manifest require exact-head owner approval.
Ordinary policies, tools and integration/test workflow repairs are reviewed
against the attached task automatically. Owner can post
`/agent approve-policy <current-head-sha>` or click the authenticated Telegram
approval button. New heads invalidate approval. Local agents cannot approve or
resume on the owner's behalf. PR files cannot enroll keys or change this gate.

After planning, use `git_workflow(action="start")` to pull dev again before a
unique feature branch is created. A changed base requires plan reassessment;
acknowledge only after reviewing the changed plan. Independent clones prevent
concurrent sessions from sharing an index, branch, or working tree.

Feature sessions commit focused and pre-commit checked changes to their own
branch and open a PR to dev. Verified publication automatically hands ownership
to the Pi, archives the task, and refreshes clean dev. Finish with the PR link
without waiting for review, approval, CI or merge. Publication failures retain
local ownership; `handoff` recovers an interrupted handoff and `sync` retries
pending dev refresh. Dirty canonical work is preserved. Each next feature has
its own task ID and changelog fragment even while earlier PRs are pending.
Handed-off branches prohibit local corrections. Optional
`git_workflow(action="feedback", pr=<number>)` reads a selected PR's status.
Local cleanup prunes remote references and deletes only proven merged, unused
branches reachable from dev. Unknown, active and squash-only unique local work
is preserved and reported; the Pi safely cleans verified merged remote branches.
Each reviewer is a fresh read-only session separate from the candidate authors.
Confirmed reviewer defects start a fresh Pi correction agent followed by
independent review, repeating until approved. An unfinished source assessment
continues in fresh read-only invocations of the same immutable assignment; it
does not request code changes or owner approval. Typed source protocol 1 binds
the full task, head, dev base, producer, companion inventory, complete capture
and individual scene evidence to durable assessment checkpoints. The host
records served pages separately from substantive file/evidence assessments.
Every changed source, required current companion item and original full PNG
must be assessed, followed by an independent whole-task and cross-file judgment.
Both the controller's private pending record and the host's durable pending
registry prevent a queued or interrupted review from merging. Terminal
publication recovers missing check/comment/correction side effects using exact
decision identities and correction-row compare-and-set; newer findings and
owner/coordinator holds remain authoritative. Source-review tables are additive
in the existing control database; review initialization and completion preserve
checked-renewal receipts, correction records and later coordinator holds.
Ordinary preparation keeps its existing prompt unless a compatible worker selects
source protocol 1. Coordinator read-only preparation keeps the ordinary context
without beginning a source review or acquiring writer authority. Coexistence
regressions create a concrete later correction through the control API before
checking that review completion preserves its identity, contents and merge block.
Legacy five-field verdicts cannot establish source completeness. The separately reviewed worker adapter supports
only the pinned approved legacy controller closure during bootstrap, retaining
its host merge exclusion until the complete independent result is reconciled.
Material PR feedback is observed again before final publication and retirement.
New input creates an explicit pending generation, preserving the earlier
assessment history. An exact completed decision may be superseded only with
its bound receipt while the host still owns the pending publication; this
grants no approval and cannot replay a retired generation transition.
Only genuine user questions, owner veto/cancel, invalid provenance or subscription
and authentication limits pause work. Rejection messages have no approval buttons.
Fast CI, independent review and protected exact-head owner approval still apply.

The private subscription worker is maintained in self_hosting's
`self_host/services/github_agent`. Airflow calls its Unix socket every minute
and updates the underlying Codex executable Sunday at 04:00 Europe/Moscow.
The Pi performs bounded source operations only; GitHub runs tests and renders
visual scenes. Up to ten jobs run concurrently with serialized merge decisions.
Fast checks and the App's exact-head/base review gate merges. Integration runs
are monitored after merge and repaired through reviewed PRs until matching
scenario groups and transports are green. No OpenAI API key is used.

Recommended implementation choices go to a separate read-only plan resolver,
which approves them only when they align with the complete user request. Genuine
questions and protected exact-head owner decisions retain their authenticated
answer path. A singleton maintenance orchestrator starts at most every thirty
minutes while PRs are open, preserves failed patches and task evidence, and
may propose source repairs in analytics_toolkit and self_host. Independent
review and GitHub validation gate publication and merge; the Pi runs no checks.
An idle trusted supervisor activates newly merged, owner-approved automation
paths on main only after verifying their merge identity, signatures, task
binding, fresh authorization and exact-head/base checks. Package code remains
on dev until an explicit release.

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

Normal `version-bump` calls record a feature fragment or add the supplied
summary under `## Unreleased`; they never bump versions, regardless of entry count.
Documentation-only calls retain their no-bump behavior.

An explicit `release-workflow --action merge-dev` request prepares one new
version through a signed PR to dev, before validation/TestPyPI. Supply
`--summary "Release summary"` when no unreleased entries exist. The optional
`summary` Python/MCP argument is keyword-only; existing action/root arguments
and action names remain unchanged. Re-enter the same action after review and merge.
The structured pending response includes the PR link and continuation instruction.

Private `.git/atk-release.json` state binds the release task, source SHA, version,
branch, PR, candidate SHA/tree, workflow runs, and completed stages. Retries reuse
the candidate and retained verified artifacts; they do not blindly recreate
TestPyPI branches or GitHub releases. Changed dev content requires another
preparation PR and fresh validation. Before pushing a temporary TestPyPI branch,
the intended SHA and pending push are atomically journaled, and the signed commit
is retained under a candidate-specific private local ref. Resume retries that
exact commit if the push never reached origin. A remote ref is adopted only after
checking its single candidate parent, project-name-only metadata change, full
changed-path list, and agreement with any journaled SHA. Legacy state without a
SHA can recover through the same checks before publication reconciliation.
An established ref that disappears blocks recovery, except after a journaled,
fully reconciled leased deletion. Unpublished replacement retains the prepared
version and still requires one fresh reviewed PR. Status never prepares or increments.
Direct release scripts require the same reviewed candidate and validation receipts.
See [release instructions](../agent_docs/release.md) for the complete sequence.

Low-level release-oriented `version-bump` options remain available for metadata
preparation, including `--change-type release --force-release`. A summary can
supply notes for an otherwise empty release. Metadata-only preparation PRs use
the rolled release notes without adding a feature fragment that reopens Unreleased.
Private implementation workers add regressions and edit sources without running
programs, local pre-commit checks, tests, installs, or formatters. The trusted
coordinator verifies task identity, pinned base/head, provenance, protected paths,
and patch applicability before publishing an App-signed correction to the PR.
For ordinary feature corrections, preserve versions and Unreleased notes. If the
task-owned fragment is already folded, keep its note once and its signed history;
do not recreate the consumed fragment. Before validation, verify that coordinator
metadata children also preserve these invariants instead of applying an entry-count bump.
Corrections retain current-dev repair discovery, bounded evidence handling, and
unchanged no-patch retry suppression with their fast mocked regressions.
GitHub then validates the exact corrected head through the focused regressions,
Python matrix, SDK conformance, lint, type, coverage, and artifact gates. Successful
`fast-checks`, fresh independent review, and authenticated exact-head owner policy
approval for protected paths are required before merge; earlier-head results and
approvals cannot substitute. This release feature changes protected publishing
workflow guards and controller changelog folding, so the coordinator must present
PR #7 and its final SHA for `/agent approve-policy <sha>` through the authenticated
owner flow after validation and review; agents cannot grant that approval.

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
is the canonical Python 3.11 test run; the remaining Python 3.9 through 3.14
matrix omits that duplicate and defaults to three parallel tox workers. Set
`PRECOMMIT_PARALLELISM` to a positive integer to tune it. The minimum-dependency profile runs on Python 3.9
and retains `pip check`; Python 3.8 is excluded from runtime checks. Artifact installs use
fresh virtual environments with a shared `.tox/pip-cache` download cache.

Successful stages receive private 24-hour receipts keyed by the exact working
tree, command definition, toolchain versions, and parallelism. An interrupted
or corrected run reuses only exact matches and reports each stage as executed,
reused, failed, or invalidated. The run keeps its original source and toolchain
identity: drift observed before or after a stage, during reuse, or before final
certification stops validation without relabelling earlier receipts. Version 2
receipts replace legacy receipts, and commit readiness rechecks the toolchain.
Keep any edits and rerun checks on the resulting tree. This also applies when
the coverage command accepts a managed target increase that changes tracked
metadata; the complete pre-commit run must validate the updated tree. Boundary
checks do not provide an immutable execution sandbox or detect a temporary edit
that is fully reverted between observations. The manual coverage command keeps
its review-and-rerun behavior. The compatibility `--quick` script mode retains its
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

Feature-session `commit` and `push` return immediately after verified signed PR
handoff; the Pi owns GitHub checks. Legacy bootstrap and release operations watch the
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

The private Telegram worker also accepts `/prompt_agent <description>`, authenticates
the owner (with an owner-only confirmation button for anonymous channel posts),
validates the request in a separate session, and opens a native Codex Plan-mode
exchange through App Server with read-only source tools.
Reply to a current question or use `/reply_agent <request-id> <answer>`; use
`/cancel_agent <request-id>` to cancel. A separate agent must approve the completed
plan against the initial prompt and all user responses before another agent codes.
Questions during coding pause work and return through the same exchange. The Pi
runs no repository checks, builds or renders; GitHub performs validation and a fresh
independent agent reviews the final PR.

The agent-only validation environment retains the reviewed
`virtualenv>=21.1,<21.5` toolchain. Project setup resolves this constraint together
with tox; it does not change public package dependencies.

## Release candidate recovery

Ordinary entries do not bump versions. An explicit release prepares one new
four-part version through an independently reviewed PR; see
[release instructions](../agent_docs/release.md) for the publishing sequence.

Re-enter the same release action to resume its candidate/version. TestPyPI retries
adopt matching publish.yml workflow_dispatch runs for the exact temporary ref and
SHA. Rejected dispatches are retryable; uncertain or interrupted attempts retain
a five-minute deadline across invocations, then recheck GitHub and index files
before a replacement dispatch. API/index failures block retry. The release-state
lock excludes concurrent dispatchers. A late original dispatch can still produce
a duplicate run. Saved runs and published files require retained matching artifacts.

If publication stops after PR creation, recovery discovers the existing preparation
PR across open and merged states before attempting another push. It verifies the
release task, branch and base, then resumes durable Pi handoff using the original
signed publication SHA. Corrections made by the Pi are never overwritten by the
stale author head. Conflicting receipts, ambiguous PRs, closed unmerged PRs and
GitHub discovery failures block recovery; only confirmed absence permits publication.

A corrected preparation PR that merges with successful exact-head fast-checks
and agent-review keeps the same version, PR and final reviewed notes. Recovery
archives the old state and clears validation receipts and publication bindings.
Current dev must match the final reviewed tree, and package/README versions and
consumed release notes must remain consistent. Old active or uncertain publication,
published artifacts and incompatible refs block adoption into that same PR.
Reviewed metadata is read with surrounding whitespace stripped and one trailing
newline restored per file. An empty `Unreleased` section is accepted when the
versioned release notes remain present and all changelog fragments are consumed.
Newer dev content automatically starts a replacement preparation PR under the
same release task after publication reconciliation. An unpublished, untagged
version is reused with combined notes; published or tag-reserved versions advance
to the next unreserved four-part version. Published artifacts and their refs stay
intact. Journaled generations recover interruptions without duplicate preparation
PRs or manual state deletion, and require fresh review/checks and release validation.
If recovery is interrupted after journaling a replacement workspace, the next
resume clears the predecessor's validation receipts before publishing that workspace.

The toolkit task's companion Telegram formatting plan was delivered separately.
The supplied private operator attestation records task `100fd816ae2f`, Telegram
messages 55–57 (canonical message 57), and plan SHA-256
`dffec0da15ac21935897d533ca0d37e5e18bdea3cb905a8401c36507cac0c03a`.
Its source bases are self_host `f26ec8f4d6def43f04acaa00d303976a7d6b7b04`
and CIAN `ed8efdea19388a7590ff60d4bbe620b098b57bb2`. This is delivery
evidence, not toolkit approval. Companion code and later companion command
extensions are outside this PR; the toolkit formatting handoff covers GitHub-agent
alerts, plans, questions and bridge responses in readable English HTML.

## User documentation wiki exporter

`wiki_sync.py` exports validated user documentation to the separate GitHub wiki.
It uses only the Python standard library; source Markdown remains authoritative.
See [wiki synchronization setup and retries](../docs/WIKI_SYNC.md) for release
activation, credentials, validation, and ownership of generated pages.
