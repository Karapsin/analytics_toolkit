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

The controller's `merge_selected` entrypoint lets the worker merge reviewed,
unowned PRs while unrelated jobs continue. Its explicit PR selection never grants
merge authority: current signatures, task provenance, review/base, fast CI,
private holds and exact-head protected approval still apply. An empty selection
merges nothing. Post-merge cleanup also receives the worker's live and unpublished
request branches, retaining its existing unknown-branch and exact-head guards.
The existing `merge(...)` entrypoint remains compatible for idle callers.
This source capability needs independent review, GitHub checks and the owner's
exact-head protected decision before controller activation.

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

Long histories use an immutable, content-addressed task archive linked from the
PR. Its readable pages preserve every user message, agent question, full answer,
plan and image record; canonical chunks preserve the complete signed task data.
The reader checks each chunk and the archive's exact layout before verifying the
existing head-bound task digest. Missing, changed, stale or mixed attachments
fail closed. Earlier inline attachments keep their original signatures and
remain supported.

Archives live under `agent-task-evidence/v1/<task-digest>` tags and contain only
task data. They neither change the candidate tree nor invalidate its visual
receipt. They cannot grant approval, resolve owner questions or clear a hold.
Publishers validate the final body and archive bounds before mutations, retain
the exact patch on failure, confirm archives before feature pushes and reconcile
interrupted writes without replacing refs. Keep these tags as audit evidence;
normal merged-branch cleanup must not remove them.

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
The outbox suppresses non-question request status notices explicitly marked
`telegram_feature=false`, including queued retries with different revision keys.
Suppressed rows retain their original payloads and delivery receipts with `sent=-1`;
no request, checkpoint or answer is removed. Owner questions (`:waiting`) and
correction requests after task-context rendering fails (`:context-blocked`) remain
deliverable even for local requests, without requiring a separate waiting notice.
Authentication requests, approvals and Telegram feature updates remain deliverable.
Each pass scans at most 200 ready rows and returns at most 20 delivery events.

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

An automatic conflict job may inherit a protected controller entry only when the
PR left its ancestor unchanged and the staged entry exactly matches current dev.
A protected merge that requires any other resolution records a private correction
request bound to the original head and task digest. The next reconciliation sends
it through fresh correction planning and independent plan review instead of
retrying the same blocked conflict publication. Sensitive paths remain denied;
owner/security holds, fresh implementation review, fast CI and protected exact-head
approval still gate the correction. This routing records no owner answer or
approval and sends no operational Telegram alert.

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
[self_host commit 0c1f1abea3f70955f63d6fbca7b06e4cc3386dde](https://github.com/Karapsin/self_host/commit/0c1f1abea3f70955f63d6fbca7b06e4cc3386dde).
Require successful exact-commit Repository Checks, immutable source/test evidence,
a fresh independent review and the isolated worker rollout rehearsal before host
activation. It includes the complete checkpoint, question/answer, label, capacity,
notification and evidence fixes from `40707720a5e1b4ac8c03ec6a6d96546a5ebc10e4`;
no companion features or private task records may be discarded.

The activation includes the backward-compatible task-context reader from PR #15
before the worker starts. Legacy signed tasks remain valid; new plan/image evidence
uses its own signed digest, and questions remain visible beside owner answers.
Missing actual plan/image bindings still require authenticated evidence; reader
compatibility does not fabricate them. Long v2 records use a lossless compressed
hidden payload while retaining all visible messages, plans, questions and answers.
The decoder bounds output to 512 KiB and rejects malformed, truncated, trailing or
duplicate records; canonical signed digests remain unchanged.
PR #15 retains its entire saved checkpoint
and remaining implementation scope.

Supervisor-restored historical approvals live in hash-bound private evidence.
The manifest can index them by task ID for future corrections, but reuse requires
the same original task, prompt and complete correction prefix through that exact
approval. Later corrections retain precedence. The worker adds evidence only to
the new publication context, preserving the original signed record and user
answers; it never invents another approval.

Reference validation excludes only the recovered plan's exact sentence beginning
“Before handoff, flag unresolved references such as” and ending “of the missing
requirement.” from its correction-message scan. That sentence documents quoted
plan/image marker examples. Stored chronology, signed bytes and rendered evidence
remain unchanged. Other references, including quoted or backticked image markers,
still require actual evidence or an authenticated replacement; actual plan
approval requests still require their chronological approval binding. This reader
repair requires independent review, GitHub checks and owner exact-head protected
approval before controller activation; it does not release existing holds itself.

The worker allows ten concurrent jobs. A separate read-only plan resolver checks
recommended implementation choices against the complete original request and
corrections. It approves only aligned choices and records the automated decision
separately from owner answers. Missing evidence, genuine unanswered questions,
owner vetoes, credentials and protected policy decisions keep their actual owner
answer path. Both questions and answers remain visible in task/PR evidence.

The owner-requested eligibility rule is: any open analytics_toolkit PR qualifies;
an open self_host PR qualifies only when it affects GitHub agent functionality.
This includes human-created and untracked PRs. Review complete current-head
changed files, including rename origins: direct agent service/deployment changes
qualify; shared tooling, dependencies, workflows, tests and documentation need
source evidence of agent impact. Titles and authorship do not establish relevance.
Bind classification to repository, head and inspected comparison; invalidate changed
comparisons and retry incomplete evidence. Confirmed eligibility in either
repository permits maintenance even when evidence elsewhere is unavailable.
Generated maintenance context retains the original request and both chronological
eligibility answers, so the later self_host clarification takes precedence.

One global maintenance orchestrator starts on the first eligible tick, then no
sooner than 1,800 seconds after its persisted previous start. Its inherited process
lease prevents duplicates across supervisor restarts and long-running jobs.
Pause controls and worker capacity still apply; busy ticks create no catch-up
backlog. It may edit private source checkouts of analytics_toolkit and self_host,
preserves complete failed patches and evidence, and exits immediately when no
concrete issue exists, releasing the lease. It runs no tests, builds, installs,
services or visual scenes on the Pi. A fresh read-only reviewer assesses both
patches; GitHub supplies validation. Companion proposals require scoped App access
to self_host, independent review and all required exact-head workflows.
Source merge never directly updates the installed worker.

Companion trigger discovery remains a coordinator-owned dependency in the existing
self_host writable assignment. Locally tracked companion proposals do not provide
a complete inventory of eligible self_host PRs. Keep this dependency unresolved
in publication and review evidence until immutable implementation source/tests,
commit identity, independent review and exact-commit Repository Checks are
available. Actual daemon-tick regressions must cover repository-qualified
inventories, human-created PRs, rename origins, shared-file impact, comparison
changes, incomplete evidence and singleton/cadence behavior. Operational acceptance
also requires trusted deployed-revision metadata and the isolated GitHub rollout
rehearsal. This ATK context change does not establish that trigger implementation.

Protected controller activation still requires successful final-head checks,
fresh independent review and the authenticated owner's exact-head
`/agent approve-policy <sha>` decision. Models cannot post that decision. After
initial activation, the idle trusted supervisor projects only allowlisted
newly merged automation paths to main. It reconstructs the merge tree from the approved head and the actual previous
dev, validates the recorded squash/merge tree and current-dev ancestry, and rechecks owner authorization
and signed task binding immediately before a non-force main push, and retains
failed activation for retry. Independently changed main files block projection.
Package code stays on dev until an explicit release; historical merges are not
replayed. The private activation ledger is included in orchestrator evidence.

Initial controller bootstrap must include the complete reviewed runtime from the
approved head: controller.py, task_context.py, automation_promotion.py,
orchestrator_policy.py, prompts/orchestrator.md, and the repair_evidence.py,
repair.schema.json and prompts/repair.md dependencies under `.github/agent/`.
The old main snapshot predates those repair dependencies. Initial approval and
independent review must cover this complete bootstrap; package, release and
unrelated policy paths are not part of that copy. Subsequent automatic updates
retain their conservative main-file comparison.

Initial deployment records rollback revisions and preserves databases,
checkpoints, questions/answers, credentials, the VPN wrapper and schedules.
After the orchestrator starts, its supervising operator monitors its actions,
intervenes only on malfunction, and stops once all PRs have merged.

### Recovery acceptance for the documented worker

Inventory every active and queued analytics_toolkit review and correction on
`kardinal@ssh.karapsin.com:2228` before initial deployment.
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

Verified publication recovery and correction-question recovery share one atomic
pause-retirement operation. It matches the observed repository, PR, head, reason
and creation time. A replacement hold or a current-head owner veto prevents the
deletion, including when it arrives during enrollment verification or after the
correction pause is read. Evidence and cancellation holds survive restart recovery.
Recovery leaves displayed labels to normal reconciliation and never records
protected approval. Local signed-PR corrections do not need App feature enrollment.

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
Airflow schedules and ten-worker limit.

Reviewers can inspect a host-owned immutable evidence snapshot for companion worker
and bot changes. Its manifest binds source/test files to hashes, repository commit
IDs and exact-commit GitHub checks. Missing or invalid evidence cannot be replaced
by an author's claim. Protected approval notices use an independent concrete change
assessment for the current head and task digest, never the starting prompt alone.


Orchestrator correction publication enrolls each independently reviewed revised
context while retaining the original digest. Recovery of the specific
host-context enrollment mismatch requires the companion's durable artifact,
original signed-head and exact plan checks, followed by final-head metadata
verification. The controller compare-deletes only that exact head/reason with
the supplied enrolled digest and no current veto; it leaves labels to normal
reconciliation and grants no protected approval. Compatible companion publication
recovery is implemented in self_host
`14f3db8820e9ec353e1cf831932ed52da05cc164` and subsequent runtime commits.
The immutable recovery evidence records independent review and successful
[exact-commit Repository Checks](https://github.com/Karapsin/self_host/actions/runs/38727228122),
including an isolated rollout rehearsal. Actual-controller fixtures cover deployed
`fb62e04558ad3d5cdaf8a9ff1eeb5527238d21f3` and proposed
`becefe94f16691e5132f7239dad7f2763133d3b5`; capability-gated digest passing
preserves the older controller contract. This completed recovery dependency is
separate from pending companion trigger discovery. Deploy compatible reviewed
controller readers with separate isolated rehearsal; protected controller
activation still requires the owner's exact-head approval. Neither resolver
decisions nor companion recovery evidence releases PR #25's provenance hold.

[Documentation overview](README.md)
