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

If refreshing a tracked run’s metadata fails with an upstream `gh api failed:`
HTTP 500–599 error, the controller retains its existing pending status and unresolved
needs, retries that immutable run ID on later reconciliation, and continues normal
authorized PR processing. This recovery applies only to the metadata fetch; other
errors, including authentication failures, malformed metadata and monitor state,
still fail reconciliation.

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
checks reject unresolved image references and “Implement the plan” without its
plan or a written replacement. See [agent task evidence](../agent_tools/README.md).

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
`b817a0541a59bb028d42ff9c243d503a39d62ba1`; inspect its immutable source/tests
and `operator/companion-b817a0541a59-ci.json` receipt. It is staged, not active.
Exact-commit green CI does not establish independent review, owner approval or
successful rehearsal.

The supplied `operator/atk-worker-rollout-b817a0541a59-receipt.json` and its
rehearsal script record an isolated successful source installation, worker restart,
claim/checkpoint recovery, ten-worker scheduling, owned-PR exclusion, unpublished
branch preservation and source-only rollback retaining answers arriving after
rollout. Reuse this evidence only for unchanged, hash-matching selected source
and deployment steps. Repeat affected steps when either changes. This evidence
covers worker recovery and scheduling; controller security checks and live
Telegram/reply/notification acceptance remain separate gates.

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
candidate SHA, procedure, assertions, restart/rollback results and evidence hashes.
Missing or failed rehearsal evidence blocks activation. Deploy compatible readers
before enabling new writers; preserve poller ownership and unrelated Airflow state.
Merge the checked, independently reviewed toolkit head with its exact-head owner
approval, then mirror only the authorized controller allowlist to main before
activating the companion. Verify the live revision, scoped capability, wait recovery,
authenticated GitHub/Telegram answers and notification filtering. Failed acceptance
restores prior source while retaining runtime state and newly received answers.

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
