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

Rejection records what changed, why it was rejected and the required corrections
on GitHub. Routine rejection and CI/repair alerts do not notify Telegram. A fresh Pi correction agent
plans the correction, a separate agent reviews that plan, and another agent edits.
Fresh independent PR review follows every changed head. This cycle continues until
approved, without an arbitrary retry cap. Rejection alerts have no buttons and
require no resume command. Routine plan, feature and correction choices use the agent’s recommended options,
recorded as automated choices under the owner’s standing preference. Missing
evidence/access and protected exact-head approvals remain blockers;
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

Up to ten Codex jobs can run concurrently. A PR has one writer and merges are
serialized. Rebase and changelog folding precede final review and protected owner
approval. Local agents finish after checked, signed PR publication and task-context
attachment; the Pi owns all later corrections, CI, approvals and merges. A durable
handoff permits the local clone to refresh clean dev and begin another planned task
while the previous PR remains pending. Offline machines refresh at their next start.

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

The companion worker dependency is staged, not active, at
[self_host commit 87b7675978243d815b5b3902fc69cc2a241cf0b0](https://github.com/Karapsin/self_host/commit/87b7675978243d815b5b3902fc69cc2a241cf0b0),
with successful [exact-commit Repository Checks](https://github.com/Karapsin/self_host/actions/runs/38450043975/job/115478288593).
The immutable `self_host-task-evidence-87b767597824/` source/test snapshot and
`operator/companion-87b767597824-ci.json` source hashes must accompany controller
review. It supersedes ancestors `b817a0541a59bb028d42ff9c243d503a39d62ba1`
and `0ae2ffc5ce5892bedd4a37f627dc9b190958e27b`;
retain the earlier receipts as historical evidence. Toolkit does not duplicate
worker code or remove any companion features. CI receipts are not owner approval.

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
Airflow schedules and ten-worker limit.

Reviewers can inspect a host-owned immutable evidence snapshot for companion worker
and bot changes. Its manifest binds source/test files to hashes, repository commit
IDs and exact-commit GitHub checks. Missing or invalid evidence cannot be replaced
by an author's claim. Protected approval notices use an independent concrete change
assessment for the current head and task digest, never the starting prompt alone.

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
as automated decisions, never fabricated owner answers. Credential/access
requirements, cancellations, vetoes, independent review and protected exact-head
approvals retain their existing enforcement.

[Documentation overview](README.md)

## Recovered evidence and staged rollout

The RPI task `33ad72f4abee49ca97e6d058e9f81a52` retains its earlier attachment at
`agent_tools/task_evidence/33ad72f4abee49ca97e6d058e9f81a52/history/context-before-native-recovery.json`.
Its historical digest verifies that record; it does not authorize a corrected head.
The current signed context supersedes the paraphrase's approval binding with the
exact native plan, including its final newline. A separately unapproved assistant
provenance record identifies the recovered thread, plan turn/item, owner approval
turn/message and immutable source hash. The earlier assistant text remains in the
historical attachment. Subsequent owner replies are recorded verbatim and paired
with the full questions and offered options. Answers longer than 1,024 characters
link to their visible chronological correction rather than repeating it; the
signed payload and parser retain the complete text.

The coordinator owns companion implementation, rehearsal and deployment in its
separate self_host checkout. The pinned dependency candidate is
`87b7675978243d815b5b3902fc69cc2a241cf0b0`; inspect its immutable source/tests
and `operator/companion-87b767597824-ci.json` receipt. It is staged, not active.
Exact-commit green CI does not establish independent review, owner approval or
successful deployment rehearsal. This descendant applies the genuine-input
guard before native recommended answers and before coding recommendations or
tooling retries. Mixed tooling/evidence batches remain unanswered, retaining
options, request identity, revision and checkpoint. The snapshot's
`test_task_evidence.py` covers fresh native/coding holds and reconciliation,
routine recovery, legacy unlabeled-question replanning, authenticated GitHub
answers and visual rejection. `test_checkpoint_bytes.py` covers exact saved bytes
through replanning and independent review. Final independent review must inspect
this pin; ancestor reviews do not approve its changed code.

The receipt links successful Repository Checks run `38450043975`, job
`115478288593`, to the exact candidate and declares hashes for its workflow,
`agent_tools/workflow.py`, service sources and rehearsal test. The workflow's
`run-checks --level prepush` invokes `unittest discover -s agent_tools/tests -v`,
which discovers `test_worker_rollout_rehearsal.py`. Its synthetic rehearsal
installs the candidate Python sources into disposable storage, starts the real
worker against a legacy request database with external writes mocked or absent,
and asserts ten-worker capacity, active branch retention, checkpoint/decision
preservation and source-only rollback retaining a later answer. This bounded
test does not demonstrate all deployment steps: reader-first activation,
routine/protected holds during rollout, partial notification delivery,
exclusive writers, compatible configuration rollback and restart after rollback
still require verified evidence for the exact final candidate.

The earlier `operator/atk-worker-rollout-b817a0541a59-receipt.json` and its
rehearsal script remain historical evidence. They cannot establish a successful
rehearsal for changed `87b767…` source. Repeat affected steps and record a
hash-bound exact-candidate receipt before activation; do not infer rehearsal
success from green CI or this plan review.

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

The immutable `operator/pr-15-authored-fragment-fold.json` receipt records the
original author fragment followed by its trusted metadata fold. Retain the folded
CHANGELOG entry. A new correction fragment describes only this correction;
recreating the consumed feature fragment would duplicate its entry.

[Documentation overview](README.md)
