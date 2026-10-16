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

Rejection records what changed, why it was rejected and the required corrections
on GitHub. Routine rejection and CI/repair alerts do not notify Telegram. A fresh Pi correction agent
plans the correction, a separate agent reviews that plan, and another agent edits.
Fresh independent PR review follows every changed head. This cycle continues until
approved, without an arbitrary retry cap. Rejection alerts have no buttons and
require no resume command. Routine plan, feature and correction choices use the agent’s recommended options,
recorded as automated choices under the owner’s standing preference. Missing
original evidence/credentials and protected exact-head approvals remain holds;
coordinator source-access and CI-log failures retry operationally;
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

Up to ten Codex jobs can run concurrently. A PR has one writer and merges are
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

The trusted controller exposes `merge_selected(gh, app_id, work, *, pr_numbers,
active_branches)`. The worker selects ready unowned PRs while unrelated jobs run,
excluding every worker-owned PR. An empty selection merges nothing. Selection
only filters the merge loop: provenance, exact-head protected approval, vetoes,
task verification, current-dev review, fast checks, fragment folding, base rechecks,
advisory dispatch and expected-head merge requests still apply. Cleanup receives
queued/running unpublished correction and Telegram-feature branches and retains
its complete open-PR inventory. The original three-argument `merge` remains
compatible; workers with an older controller use the idle-only merge fallback.

## Updating trusted automation

The current evidenced companion dependency is
[self_host commit ab7f83cddb6981ee380d3ae76fbc51ba1a41c87a](https://github.com/Karapsin/self_host/commit/ab7f83cddb6981ee380d3ae76fbc51ba1a41c87a).
The manifest selects `operator/worker-reference-replacements-ab7f83cddb69-active.json`.
Its `sources` map retains complete source/test lineage and its `source_files`
map identifies the installed module hashes. The three changed files are also
supplied in `operator/self-host-reference-replacements-ab7f83cd-complete-source.json`.
Successful [exact-commit Repository Checks](https://github.com/Karapsin/self_host/actions/runs/38931069859),
independent source and operational review, isolated rehearsal and the active
runtime receipt are bound to this pin. Inspect the complete source and activation
receipts together; evidence of this worker's activation does not approve PR #15.

The `0c1f1abea3f70955f63d6fbca7b06e4cc3386dde` source/CI/review receipt at
`operator/companion-0c1f1abea3f7-ci-review-and-source.json` remains historical
lineage. It retained checkpoint reconciliation, questions/answers, labels,
capacity recovery, notification filtering, request-aligned plan resolution,
singleton maintenance and historical-plan recovery. Later descendants also retain
unlocked complete visual review, source-only maintenance and rollout safeguards.
These ancestors are historical evidence, not active dependency selections.
Toolkit does not duplicate separately owned companion implementation.
CI receipts are not owner approval.

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

Reference validation excludes the recovered plan's exact sentence beginning
“Before handoff, flag unresolved references such as” and ending “of the missing
requirement.” from its correction-message scan. That sentence documents quoted
plan/image marker examples. Stored chronology, signed bytes and rendered evidence
remain unchanged. Other references, including quoted or backticked image markers,
still require actual evidence or an authenticated replacement; actual plan
approval requests still require their chronological approval binding. A later
recovery message reproducing the complete exact approved plan may quote its
already-bound earlier approval without creating another approval request.
Only that exact quoted message is resolved against the earlier plan; other
quoted requirements and new instructions still block when unresolved. This reader
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
behavior when simplifying test fixtures. The scoped-merge regressions separately
cover authorization and signed task context, current-base review and fast CI,
fragment folding and publication races. Reconciliation retains dev's structured
`patch`/`blocked`/`no_change` repair outcomes and retry fingerprints; empty repair
output is not a publishable patch. Preserve dev's repair regressions and changelog
history alongside the task's previously folded feature entry.

After reviewed activation, correlate every inventoried item with its current PR
head and request revision. Observe each eligible review/correction advance or
complete, or record an explicit authenticated owner, provenance or allowance hold.
Verify stale claims recover and PR writer ownership stays exclusive. Fifteen
minutes of inactivity is diagnostic; retain the thirty-minute model deadline.
Record acceptance for the entire inventory. Missing metadata, queued checks,
failed gates or unexplained non-progress block operational completion. Integration
success remains advisory. MinIO repair PR #17 is already merged into dev; retain
its pinned-source build configuration, licenses and service-free regressions.

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

## Task evidence and Telegram preferences

Version-2 task attachments retain exact assistant questions and offered options
paired with actual chronological answers, the exact assistant plan, approval
messages, image descriptions and content-addressed PNG hashes. Reviewer tools
inspect the images from the immutable candidate checkout. An explicit owner
replacement can settle a missing requirement; it never claims the original
image was inspected. Version-1 signatures remain compatible. GitHub owner replies to Telegram
questions use the exact request ID, revision and current PR head printed in the
question notice; stale replies and other authors are rejected. Local handoff
checks reject unresolved image references and “Implement the plan” in both
the starting prompt and every correction. A starting-prompt reference requires
an explicit exact-plan binding (`approve-plan` with
`message_source: "starting_prompt"`) or a later actual owner replacement.
Unapproved plans and unrelated correction approvals cannot settle that hold.
The binding is visible in the signed v2 evidence; historic signed records remain
readable without rewriting. See [agent task evidence](../agent_tools/README.md).

Telegram sends owner approval/authentication requests, implementation status for
features requested through Telegram, PR merges into dev, dev promotions into main
and successful production releases. Routine local-PR correction, rejection and
fast-CI chatter remains in GitHub and private worker records. Explicit `/help` and
`/status` responses remain available. Suppressed notifications are retained with
a private suppressed state; older unsent alerts are retired without replay.

Recommended options resolve routine planning, implementation and correction
choices automatically, including existing question holds. Legacy routine questions
without labeled recommendations return to fresh planning with their exact questions
and checkpoints retained; obsolete plan/review receipts are invalidated. Choices are recorded
as automated decisions, never fabricated owner answers. Genuine credential/access
requirements, cancellations, vetoes, independent review and protected exact-head
approvals retain their existing enforcement.

[Documentation overview](README.md)

## Recovered evidence and staged rollout

The RPI task `33ad72f4abee49ca97e6d058e9f81a52` retains its earlier attachment at
`agent_tools/task_evidence/33ad72f4abee49ca97e6d058e9f81a52/history/context-before-native-recovery.json`.
Its historical digest verifies that record; it does not authorize a corrected head.
The amended context binds the exact native plan, including its final newline,
only to authenticated correction 0. Correction 11 cites that historical approval;
it neither approves another plan nor replaces an unavailable image. The
unsupported second binding and image replacement are absent from the amendment.
The published v1 record remains at `history/published-v1.json` with digest
`82394e7a1b09b41255565aac080c2c00b34592c085d3c07b05a5ac869ea3a933`.
The saved v2 projection remains at `history/context-before-provenance-amendment.json`.
`provenance-amendment.json` identifies both sources and the authenticated native
proof. The supplied standing-delegation clarification remains automated evidence
in `decisions`, never a human answer, structured replacement or protected approval. A separately unapproved assistant
provenance record identifies the recovered thread, plan turn/item, owner approval
turn/message and immutable source hash. The earlier assistant text remains in the
historical attachment. Subsequent owner replies are recorded verbatim and paired
with the full questions and offered options. Answers longer than 1,024 characters
link to their visible chronological correction rather than repeating it; the
signed payload and parser retain the complete text.

The coordinator owns companion implementation, rehearsal and deployment in its
separate self_host checkout. The current evidenced dependency candidate is
`ab7f83cddb6981ee380d3ae76fbc51ba1a41c87a`; inspect
`operator/worker-reference-replacements-ab7f83cddb69-active.json`, its complete
source/hash mapping and activation receipts. The receipt records active runtime,
unchanged checkpoints and cadence, and compatible trusted controller
`c666ca597a4ead1276fba1a2ab7b220b2e457b06`. Historical staged receipts cannot
establish the current candidate's status. PR #15 remains subject to its own fresh
implementation review, exact-head CI and protected approval.

Native planning and intake share `question_policy.question_kind`. Routine
implementation choices go through the separate request-alignment resolver.
Known source/policy access failures and missing coordinator CI logs are tooling
retries after credential, approval and original-evidence guards. Native RPC
questions are persisted and interrupted without fabricated replies; intake
recovers existing tooling waits while retaining checkpoints and actual answers.
Mixed genuine-input batches, missing original images, credentials, provenance,
owner veto/cancellation and protected decisions remain held. Legacy unlabeled
routine questions return to fresh planning without invented owner answers.

The active worker retains ten slots, exact saved-source reconciliation across dev
moves, authenticated GitHub answers, claim cleanup, durable model-capacity
cooldown, mutually exclusive lifecycle labels including genuine owner holds,
closed-PR cleanup, scoped merges and complete visual work outside the global
reconciliation lock. Backend `willRetry` retains its live turn; capacity retries
do not cancel jobs by age, switch models or invent owner decisions. The complete
source/activation receipt and separately owned regression suite establish the
actual dependency; older reviews do not approve changed bytes.

The trusted controller calls `task_context.require_resolved` after the review-only
return and before any code-publication mutation. This gate also covers existing
feature-branch recovery: unresolved starting prompts or corrections cannot
apply patches, sign commits, push branches or publish PR attachments. Historical
v1 attachments remain readable and signature-verifiable; publication of an
unresolved reference requires its exact approval binding, image evidence or
explicit owner replacement. Before signing or pushing, the controller validates
all image descriptors and the resulting immutable Git tree: exactly one regular
blob, 1–524288 bytes, matching byte count, PNG signature and SHA256. Recovered
heads require a trusted signature and the expected producer, and receive the same
image checks before reuse or attachment publication; signed-head
metadata uses Git blobs rather than the symlink-resolving Contents API. Invalid
images prevent publication writes. Blocker reporting and writer release remain intact.

The collector retains its staged `git diff --cached --check` gate before patch
serialization. A checkpoint rejected for changelog conflict markers must be
corrected while retaining both legitimate entries, historical sections and the
owned fragment for trusted folding. Resubmit the corrected checkpoint through
the unchanged collector and fresh exact-head GitHub validation; the earlier
rejected checkpoint establishes neither publication nor successful checks.

The companion checks references before coding, publication and recovery writes,
retaining genuine missing-evidence holds and saved publication artifacts.
Authenticated Telegram approvals bind the displayed plan text, dev base,
correlated event and chronological answer. Revision/head-bound GitHub replies
use the same correlation rules. An unrelated reply or approval of an older
plan/base cannot approve a later plan. `test_publication_references.py` covers
these companion boundaries and correlated approval records; toolkit
`tests/repository/agent_lifecycle.py` exercises the real resolver with mocked
Git/GitHub publication operations.

The historical `operator/companion-0c1f1abea3f7-ci-review-and-source.json`
receipt retains its exact source/test map,
independent review, prepush fingerprint and successful Repository Checks run
`38627217757`, job `116069307512`. The current active receipt instead binds run
`38931069859` to `ab7f83cddb6981ee380d3ae76fbc51ba1a41c87a`, its independent
reviews and isolated rehearsal. It describes source-only rollback preserving later
answers, checkpoints, private decisions and unrelated source. These receipts
are deployment evidence for their named source only; they grant no current-head
toolkit approval or blanket permission for another activation.

Before activation, require fresh independent review of both final candidates,
required GitHub checks and protected owner approval for the corrected toolkit
head. Follow self_host `AGENTS.md:25–26`: the coordinator must also perform an
isolated off-Pi rehearsal with the exact candidate, copied or synthetic legacy
state, isolated configuration and mocked external writes. Exercise reader-first
rollout, routine and protected holds, interruption during active work and partial
notification delivery, restart/checkpoint restoration, claim recovery and
ten-worker scheduling. Rehearse source-only rollback by restoring previous selected
source and compatible configuration and restarting while preserving databases,
queues, approvals, evidence, checkpoints and ignored runtime state.

Rehearsal passes only when identities/revisions remain consistent, pending work
survives restart and rollback, notification delivery resumes without duplicate
completed events, and stale approval cannot authorize a changed head. Record the
candidate SHA, procedure, selected source/configuration hashes, workflow/run/job
identity where applicable, discovered rehearsal tests, assertions and
restart/rollback results. Verify the receipt against the exact final pin and
every selected source hash; changed candidates require renewed evidence for
affected steps. Missing, failed or unverifiable rehearsal evidence blocks activation.
Deploy compatible readers before enabling new writers; preserve poller ownership
and unrelated Airflow state.
Merge the checked, independently reviewed toolkit head with its exact-head owner
approval, then mirror only the authorized controller allowlist to main before
activating the companion. Record live acceptance of the deployed revision, scoped
capability and recovered workers; authenticated Telegram and GitHub replies;
stale-revision, wrong-head and wrong-author rejection; visible question/answer
pairs; permitted approval, Telegram-feature, dev-merge, main-promotion and release
notifications; and suppression of routine/backlog alerts. Failed acceptance
restores prior selected source and compatible configuration, restarts the worker
and retains databases, queues, approvals, checkpoints, evidence, unrelated
modifications and newly received answers.

The authorized completion set is analytics_toolkit PRs #7, #9, #10, #11, #12, #13,
#14 and #15 plus repairs needed to finish them, all targeting `dev`. The
coordinator inventories current heads, bases, agent phases, holds, review, exact-head
CI and mergeability, recovers routine waits and continues monitoring until each
target and repair has a verified GitHub merge receipt into `dev` and required
post-merge repairs are complete. Serialize merges and revalidate every corrected
head and changed dev base. Missing evidence/credentials, vetoes, provenance failures
and protected approval remain genuine holds. self_host keeps its authorized
direct-dev workflow; this task grants no authority over unrelated repositories,
package releases or main promotions.

### Complete geometry failure evidence

The scene regression retains every assertion and its existing polling limits.
On failure it reports a bounded scene/count summary followed by complete serialized
JSON and writes the same report to `.rag_index/visual-failures/<scene>.json`.
The matrix and quality jobs upload these files even when validation fails, using
`visual-geometry-failures-<environment>` artifacts with hidden files included.
The coordinator must export failed reports through the existing immutable evidence
interface as numbered JSON lines, with SHA256 hashes and the exact head/run/job
identity in the evidence index; logs alone cannot replace missing geometry.

Quality run `38495991023`, job `115631751342`, on toolkit head
`5d5aa0242ddcb7f80a05fcffcf8e1556d6434f1d` failed lint/format in the MinIO
regression and the completion geometry check. Its complete logs are indexed at
`operator/pr-15-ci-5d5aa0242ddc-115631751342-index.json`; the original failed
geometry was not retained and cannot be reconstructed from pytest's truncated
dictionary. The later trusted capture run `38499353959`, job `115642356404`,
on that same candidate passed all 46 automated geometries. Its immutable index is
`operator/pr15-5d5aa024-38499353959-capture-index.json`. The coordinator reviewed
the full completion PNG individually; this does not establish the earlier failure's
cause or individual review of the other 45 PNGs. Final content still requires fresh
GitHub checks and the complete visual review. A repeated failure requires retained
full geometry and a cause-specific fresh plan and independent review before any
visual behavior change; assertions, cases and polling limits must remain intact.

The immutable `operator/pr-15-authored-fragment-fold.json` receipt records the
original author fragment followed by its trusted metadata fold. Retain the folded
CHANGELOG entry. A new correction fragment describes only this correction;
recreating the consumed feature fragment would duplicate its entry.

[Documentation overview](README.md)

## Visual routing and complete quality diagnostics

Changes to the five agent visual/geometry helpers, production SQL Explorer,
the scene manifest, capture harness and SQL Explorer tests require the full visual
gate. Publication inspects both rename endpoints. Obtain a fresh exact-candidate
capture, inspect every manifest PNG individually (currently 46), and retain task,
patch and capture identity checks. Full failed geometry JSON is always uploaded
alongside a bounded summary. A later passing capture does not identify the cause
of an earlier failure; any UI behavior repair needs cause-specific planning and
independent review. Preserve all geometry assertions.

The canonical quality wrapper retains complete Ruff lint/format and mypy stdout
and stderr under `.rag_index/quality-diagnostics/<id>/` before parsing or debt
comparison, including malformed output and nonzero exits. Each `index.json`
binds the command, exit code, checkout revision, PR source head, workflow/run/attempt,
toolchain versions, stream byte counts and SHA-256 hashes. The quality workflow
always uploads `quality-diagnostics-<run>-<attempt>`; summaries print the index
path. The coordinator exports the complete artifact and index to correction
workers alongside exact-head check logs, without treating their content as owner
instructions. Keep debt enforcement, baseline counts and return codes unchanged.
