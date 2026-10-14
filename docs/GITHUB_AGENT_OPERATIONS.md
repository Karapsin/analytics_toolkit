[Documentation overview](README.md)

# Private GitHub agent operations

The GitHub App reviews feature PRs targeting `dev`. The subscription-backed
Codex worker runs on the private Raspberry Pi; GitHub runs all repository tests
and SQL Explorer scene captures. No OpenAI API key is used.

## Scheduling and status

Airflow owns `github_agent_reconcile` (every minute) and
`codex_weekly_update` (Sunday at 04:00 Europe/Moscow). The systemd
`github-agent.service` keeps the private socket worker running.

Connect with `ssh kardinal@ssh.karapsin.com -p 2228`. Check the service with
`systemctl status github-agent.service`. Read the fixed-operation socket status
with:

```bash
python3 /home/kardinal/projects/self_hosting/self_host/services/github_agent/client.py status
```

The App maintains a GitHub issue titled `GitHub agent integration monitor`.
It records pending and failed integration runs and the corresponding repair PRs.
A completed green replacement must cover each affected job group before the
incident is resolved.

Status includes each active job's phase, start time, last event time and model
deadline. Fifteen minutes without an event is reported as inactive, not proof of
a hang. Model execution keeps its thirty-minute timeout; timeout cleanup ends
only that job's process group, preserves diagnostics and coding checkpoints,
releases writer ownership and queues a fresh attempt.

Integration repairs receive hash-bound diagnostic artifacts for the exact run
and attempt through `read_evidence_file`, starting at `integration/manifest.json`.
Missing or expired diagnostics are listed explicitly. A repair can return a patch,
a blocked diagnosis or no change. No-patch outcomes retain unresolved incidents
and produce a deduplicated notification rather than repeating the same model job
every five minutes. A changed dev base, affected failure groups, run attempt or
artifact inventory permits another attempt. Transient artifact retrieval failures
retain normal timed retries.

If refreshing a tracked run’s metadata fails with an upstream `gh api failed:`
HTTP 500–599 error, the controller retains its existing pending status and unresolved
needs, retries that immutable run ID on later reconciliation, and continues normal
authorized PR processing. This recovery applies only to the metadata fetch; other
errors, including authentication failures, malformed metadata and monitor state,
still fail reconciliation.

The same strict upstream error classification applies inside repair evidence
inventory, including run metadata and artifact pagination. Only that inventory
call is deferred: already discovered review, correction, conflict and metadata
tasks are returned, and monitor progress is saved. Unresolved needs and retry
deadlines remain intact for the next reconciliation. A transport failure creates
no evidence fingerprint, no blocked/no-change outcome and no notification.
Authentication, malformed evidence and provenance failures still fail closed;
errors outside the inventory call are not covered by this recovery.

## Enrolled machines and policy changes

Automatic PR handling accepts only the repository owner's PRs with an enrolled
machine signature, plus this App's signed repair commits. The private host owns
`~/.config/github-agent/allowed_signers`. Keys are explicitly enrolled; neither
GitHub account keys nor files in a PR modify that registry. Private signing keys
remain on their originating machines.

Every feature PR attaches the starting user prompt and subsequent corrections,
with credentials redacted. The digest is bound to its signed head. Every review
uses a fresh read-only Codex session separate from its authors. The reviewer
evaluates the complete diff against the task and existing quality requirements.

Owner approval is retained for controller code/prompts, its security configuration,
dispatch/capture/publishing workflows and required-check classification. Ordinary
instructions, agent tools and integration/test workflow repairs can be approved
automatically. Post `/agent approve-policy <current-head-sha>` or use the owner-only
Telegram approval buttons. A new head invalidates approval. The host verifies the
Telegram owner, channel, message, callback authentication and current PR head;
the App posts a transparent audit comment. Local agents cannot approve for you.

`agent:needs-user` reflects an actionable approval after current review and fast CI
pass, or a private owner-input/veto
hold. A successful Telegram approval reconciles it immediately; discovery and
merge passes also repair it from current authorization and private holds. A
failed label update retries without losing the approval. A concurrent new head
retains the label until its own decision. Label cleanup preserves unrelated
labels and never clears a question, veto, signature, review or CI gate.

Rejection posts what changed, why it was rejected and the required corrections,
mentions the owner and sends a plain Telegram alert. A fresh Pi correction agent
plans the correction, a separate agent reviews that plan, and another agent edits.
Fresh independent PR review follows every changed head. This cycle continues until
approved, without an arbitrary retry cap. Rejection alerts have no buttons and
require no resume command. Genuine questions wait for authenticated owner answers;
owner vetoes/cancellation and invalid provenance stop work. Invalid task context
also fails closed. Older trusted
App repairs can inherit the standing integration-repair task, bound to their
immutable signed head; owner-authored PRs must attach their task.

Telegram sends brief change descriptions and links for PR merges into dev, dev
promotions into main and successful production PyPI release workflows. TestPyPI
and automation-only main mirrors are excluded. Notifications use a private,
durable outbox, retry delivery failures and deduplicate successful events.

## Merge and repair behavior

Merges require current `fast-checks` and the App's `agent-review`, against the
current `dev` base. Integration success is not a merge gate. The worker monitors
integration after merges and creates repair PRs with fast regression coverage.

Up to five Codex jobs can run concurrently. A PR has one writer and merges are
serialized. Rebase and changelog folding precede final review and protected owner
approval. Local agents finish after checked, signed PR publication and task-context
attachment; the Pi owns all later corrections, CI, approvals and merges. A durable
handoff permits the local clone to refresh clean dev and begin another planned task
while the previous PR remains pending. Offline machines refresh at their next start.

Discovery reads each changed changelog fragment from the immutable PR head before
queuing deterministic metadata folding. A malformed fragment starts a fresh
correction plan with its exact path and formatting requirement, rather than
repeating the same failed fold. The signed task context and normal independent
plan review, final review, fast CI and protected approval requirements still apply.
GitHub read failures retry without inventing a formatting rejection; genuine
owner holds remain in place.

Cleanup removes unchanged branches belonging to merged PRs after merge and daily
through existing reconciliation. Expected-head leases reject deletion races. Dev,
main, protected/open-PR/active branches and unknown or unmerged work are preserved.
Local startup prunes remote references and removes only unused, safely merged local
branches; dirty worktrees, backups and private task/audit records are retained.

## Updating trusted automation

The companion worker dependency is staged, not active, at
[self_host commit 0ae2ffc5ce5892bedd4a37f627dc9b190958e27b](https://github.com/Karapsin/self_host/commit/0ae2ffc5ce5892bedd4a37f627dc9b190958e27b),
with successful [exact-commit Repository Checks](https://github.com/Karapsin/self_host/actions/runs/38411193449/job/115343656791).
The immutable `self_host-task-evidence-0ae2ffc5ce58/` source/test snapshot and
its manifest hashes must accompany controller review. It supersedes ancestor
`51acbc647dbcef6612b4c3ee806d6bd1abfcce0d`; toolkit does not duplicate worker code
or remove any companion features.

The dependency hashes the full logical key for non-request job directories.
Execution and both status paths share `job_runtime.directory`, avoiding the
255-byte filesystem basename limit without changing logical job identities or
SQLite records. Request directories keep their existing layout. Setup runs
inside cleanup, and failure-report writes cannot skip claim completion or writer
release attempts. The pinned
`test_task_evidence.py::test_full_length_job_identity_and_setup_failure_release_claim`
covers production-length keys and setup failure. Keep legacy directories and
private checkpoints; drain active work before changing directory conventions.

PR #13 publishes the toolkit correction through its controller-owned flow,
preserving its signed provenance, task attachment and coding checkpoint.
Require final-head GitHub validation, fresh independent `agent-review` against
the current dev base and task digest, and authenticated owner exact-head
`/agent approve-policy <sha>` approval before protected controller activation.
Changelog folding or rebasing changes the head and requires renewed checks,
review and approval. Models cannot post that approval. Older toolkit successes
do not validate this correction. A changed companion commit requires new immutable
evidence, exact-commit checks and independent review.

Full companion activation belongs to the separately authorized PR #15 coordination
task and requires its fresh independent review and protected-controller approval.
PR #13 must not activate an unreviewed companion bundle. The coordinator performs
activation only after these gates permit it, records rollback revisions and
preserves databases, checkpoints, credentials, the VPN wrapper and schedules.

### Recovery acceptance for the documented worker

Inventory every active and queued analytics_toolkit review and correction on
`kardinal@ssh.karapsin.com:2228`, including PR #13 and all other current PRs.
Before activation, record service/socket status, deployed revisions, open PR heads,
queued/running jobs, request stages and revisions, retry deadlines and writer
ownership. Use trusted operational tooling to repair evidence-access failures;
do not ask the owner to supply repository files or CI logs.

After publication, run focused regressions and mandatory static/type, coverage,
artifact and Python compatibility checks on GitHub. Run the isolated rehearsal
there too, using disposable state and mocked GitHub/Codex interactions with no
production credentials or services. Cover transient inventory failures,
production-length identities, execution/status path agreement, distinct logical
keys, removal/creation/progress-write failures, failure-report write failure,
claim and writer release, timeout/process-group cleanup, restart recovery and
checkpoint retention. Reuse the pinned runtime, checkpoint, native-planning and
alert regressions. The Pi executes no tests, builds, programs or scenes; trusted
operations and live metadata verification remain permitted.

When a quality job fails, the coordinator captures its complete redacted log and
an index bound to the PR head, run and job IDs. Correct the reported task-related
findings without increasing lint or format debt baselines, then require successful
`fast-checks` on the final published head. Preserve regression assertions and retry
behavior when simplifying test fixtures.

After reviewed activation, correlate every inventoried item with its current PR
head and request revision. Observe each eligible review/correction advance or
complete, or record an explicit authenticated owner, provenance or allowance hold.
Verify stale claims recover and PR writer ownership stays exclusive. Fifteen
minutes of inactivity is diagnostic; retain the thirty-minute model deadline.
Record acceptance for the entire inventory. Missing metadata, queued checks,
failed gates or unexplained non-progress block operational completion. Integration
success remains advisory; MinIO stack repair is a separate follow-up.

Only the controller, prompts, schemas, fixed dispatch bridge, capture workflow,
and workflow classification manifest are copied to `main` for automation.
Package features stay on `dev` until an explicit release. The worker fetches
trusted control files from `main`; source under review cannot select commands,
credentials, models, or host executables.

The App key stays in the private host's `~/.config/github-agent/app.pem`.
Airflow receives neither that key nor Codex login credentials. Key replacement
uses the owner-only encrypted transfer workflow and a public recipient
certificate; never put plaintext credentials in a PR, log, or terminal output.

The weekly updater preserves the existing `/usr/local/bin/codex` VPN wrapper.
It verifies the official ARM64 download and changes the user-owned `current`
symlink only after the version smoke check. A failed update restores the previous
pointer.


### Telegram feature requests and planning

In GitHub Alerts, send `/prompt_agent <feature request>`. Anonymous channel
commands and answers require the pinned owner's **Confirm request/answer** button;
that authenticates the exact message and does not approve a feature or bypass review.
Identified owner messages are authenticated directly. No other user can allocate a worker.

A fresh read-only agent validates the request. A separate native Codex Plan-mode
session, driven through the private App Server, inspects context and asks questions
in Telegram. No prompt-only planning fallback is accepted. Answer by
replying to the latest question message or with `/reply_agent <request-id> <answers>`.
Multiple requests have independent IDs. `/cancel_agent <request-id>` invalidates
queued and running results. Replies to stale question messages are rejected.

After questions are answered, a fresh read-only agent reviews the plan against
the original prompt and chronological responses. Only an exact approval tied to
the task, plan and dev SHA allows a separate coding session to start. Changed dev
or user scope requires planning and review again. Coding questions pause work,
preserve its private patch, and return through Telegram and plan review before
implementation resumes. An invalid request asks for user correction; a rejected
implementation plan returns to a fresh planning round with the findings.

The Pi only performs source inspection/edits and lightweight trusted Git/controller
operations. It never executes repository programs, tests, builds, installs or scenes.
All heavy checks run on GitHub; the final PR receives another fresh independent
reviewer. Original prompts and answers are attached automatically with credential
redaction, bound to signed commit trailers. Request state and notifications survive
worker restarts. The subscription worker retains its VPN wrapper, model configuration,
Airflow schedules and five-worker limit.

Reviewers can inspect a host-owned immutable evidence snapshot for companion worker
and bot changes. Its manifest binds source/test files to hashes, repository commit
IDs and exact-commit GitHub checks. Missing or invalid evidence cannot be replaced
by an author's claim. Protected approval notices use an independent concrete change
assessment for the current head and task digest, never the starting prompt alone.

[Documentation overview](README.md)
