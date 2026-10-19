[Documentation overview](README.md)

# Private GitHub agent operations

The GitHub App reviews feature PRs targeting `dev`. The subscription-backed
Codex worker runs on the private Raspberry Pi; GitHub runs all repository tests
and SQL Explorer scene captures. No OpenAI API key is used.

## Failed visual capture artifacts

The trusted `agent-visual` workflow exports a separate failed-capture artifact
when capture fails. Its name binds the candidate SHA, workflow run and attempt.
It retains the run-owned session and all available raw evidence, including
geometry files written before a failing scene updates its session record.
Candidate paths cannot select arbitrary files; unsafe, changed or oversized
collections remain explicitly unavailable or incomplete. The capture job remains
failed and these diagnostics grant no visual approval.

`collection_complete` requires a retained session and at least one retained file
under `raw/evidence/`. Empty evidence directories and `raw/session.json` alone
remain incomplete, even when every available byte was safely exported. These
session-only archives preserve the finalized session and its hashes for durable
partial-diagnostic retention; they grant no model assignment or visual receipt.
Older candidate harnesses do not retain complete child stdout/stderr; their
`process_output_complete: null` means unknown output coverage. Complete child
stream capture and UI scene corrections remain separate work in PR #37.
This exporter does not change SQL Explorer or its visual harness. A new capture
produces new evidence; it cannot recover missing artifacts from an earlier run.

The separately reviewed companion consumer verifies the GitHub run, attempt,
artifact identity and archive hashes before retaining private raw bytes and a
redacted, paged model view. Publication, runtime activation and actual request
assignment require their own verified gates; merging this exporter alone does
not establish that a blocked agent has received the evidence.

## Native source validation before production activation

The separate `tests.yml` source-validation mode captures an already published
exact subject through the fixed reviewed native container transport. It does not
execute subject installs, imports, tests or rendering on the runner host. Ordinary
CI remains required; the synthetic infrastructure probe retains its separate
purpose. No capture programs or checks run on the Pi.

The workflow control must be reviewed and frozen at
`refs/tags/agent-source-validation/v1/<workflow-sha>`. First establish effective
no-update/no-delete protection, create-only publication, complete workflow-trigger
review and actual owner dispatch authority through a separately reviewed concrete
operation. Merely publishing this feature or approving a dev merge supplies none
of those operational facts. The job checks both the original and rerun actor,
the exact workflow identity, separate checkout roots and the fixed transport's
source hashes. All checkouts disable persisted credentials; candidate containers
receive no repository write, App or private controller credentials.

After those real preconditions are satisfied, dispatch the registered `tests.yml`
workflow from that frozen ref with `source_validation=true` and
`source_validation_subject=<exact-published-sha>`. Malformed or conflicting mode
inputs fail; source-mode skipped jobs cannot supply normal `fast-checks` success.
[GitHub dispatch refs are branch or tag names](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event).
Bare-SHA dispatch is not assumed.

The metadata and capture artifacts have distinct `source-validation-*` names.
The transport's private output is admitted before exposing a run-owned artifact
root. Unavailable or unsafe roots retain only safe wrapper metadata. Successful
transport evidence includes every manifest scene's geometry and full 1280x800
PNG, with independent individual image verdicts still pending. Failure evidence
remains partial or unavailable as observed. The pre-upload manifest records the
actual transport and step result; final GitHub job/run conclusions and artifact
IDs/digests must be obtained after completion and retained separately without
rewriting the uploaded original. These artifacts grant no visual approval,
production admission, main projection, runtime activation or acceptance for a
different source head.

This first mode can diagnose the already published subject. Validating an
unpublished future UI checkpoint still needs a narrowly reviewed managed
temporary-source publisher without automatic legacy capture, plus any minimal
honest external receipt support required by its consumer. Those capabilities
and the production capture activation remain separate pending work.

The pre-check emits owned metadata even when an invalid actor, mode or frozen ref
stops execution before subject checkout. A successful pre-check records only its
guard result; capture acceptance remains false until capture actually completes.
The upload retains this guard metadata when capture never starts, and includes
hidden members of safely admitted diagnostics to preserve complete originals.

## Fixed PR39 lifecycle baseline diagnostic

The separate `tests.yml` boolean `lifecycle_diagnostic` selects only two contained
diagnostic jobs: Python 3.11 with Textual 0.73 and Python 3.13 with Textual 0.89.
The source is fixed to the published PR39 baseline
`76eb8481734ec4ffb77d71ce300787caff21b860`; there is no arbitrary command,
selector, subject, profile or checkpoint input. Its complete public parent task,
public planning questions/options/replies and source provenance are retained in
`agent_tools/task_evidence/357a59078b4f423491e915d12ca37061/`.

Publish and independently review the diagnostic source, pass its ordinary CI,
and actually merge that source to dev first. A subsequent operation requires
its own concrete review, effective immutable-tag protection and the exact frozen
workflow identity under `refs/tags/agent-source-validation/v1/<source-sha>`.
Both original and rerun actors must be the owner. This source change itself
does not authorize a dispatch, synchronization to main, runtime activation or
release of PR39's prerequisites.

Candidate installation runs in the existing guarded nonroot container. Test
execution runs with networking disabled. The host imports no candidate Python
and never mounts its pristine checkout. Each profile runs the same five fixed
cases once in a baseline process with reporting only, and once in a separate
observed process. Observation preserves original calls, results and exceptions,
but can affect timing; the baseline outcome remains separate. Both fixed Ruff
commands run after the tests, retaining original failures and full phase streams.
No assertion, expectation, quality baseline or visual acceptance gate is relaxed.

Artifacts named `pr39-lifecycle-<source>-<run>-<attempt>-py<line>` retain reports,
bounded lifecycle events, framework/version provenance, original process outcomes,
stream byte counts/hashes, exact source bindings and cleanup/collection state.
The joint ceiling is 100 MiB and 256 files: 64 MiB logs, 32 MiB candidate data
and 4 MiB host metadata. Capacity loss is explicit incompleteness. Upload roots
are created only after all relevant writers stop and owned containers/resources
are confirmed absent. Otherwise only the earlier closed guard metadata is
uploadable. Original job/artifact IDs and final upload conclusions are unknown
inside the running job and remain null until genuine GitHub API collection.

A failed baseline remains a failed diagnostic even if the observed run passes.
Complete collection is distinct from test success. These artifacts grant no
visual approval, prerequisite satisfaction, production activation or ordinary
`fast-checks`/`agent-review` success. Cause-specific fixes still need a complete
reviewed plan, normal source review, real CI and all required visual reviews.

## Isolated capture execution

The capture transport keeps the Actions checkout pristine and copies the exact
candidate into a private run-owned directory. Candidate installation and rendering
execute in two disposable nonroot containers using the same resolved image ID
and `/work/.venv`. Only installation has public network access. Owned firewall
rules reject private, metadata and host destinations; rendering uses no network.
Both containers have a read-only root, temporary storage, dropped capabilities,
no new privileges and fixed resource limits. Host credentials, Actions command
files, Git configuration and the Docker socket are not mounted or forwarded.

Trusted host code runs with isolated Python outside the writable candidate. It
does not import candidate modules or run candidate Git after execution starts.
Both containers must be removed before descriptor-based collection copies regular
files into fresh host-owned artifact directories. Symlinks, hardlinks, changing
trees and oversized collections fail collection. Setup, timeout and cleanup
failures remain failures even when their diagnostics upload succeeds.

Install and capture stdout/stderr are retained separately with byte counts and
hashes, within a combined 100 MiB limit. Overflow or an unfinished reader is
explicitly incomplete. Unfinished writers are quarantined outside the uploaded
directories. A durable resource journal records mutation intent before Docker or
firewall requests. An uncertain response prevents collection and guard release;
it cannot become a successful cleanup claim. The disposable GitHub runner owns
any resources that cannot be safely confirmed and removed.

Before activating this protected workflow, run the separately reviewed optional
`visual_isolation_probe` in the `tests` workflow on the exact reviewed source.
This mode runs only the probe matrix; ordinary CI retains its usual triggers.
Its Python 3.11 and 3.13 jobs check real container identities, Git/venv permissions,
public package installation, host/private/metadata rejection, network-free
rendering, fonts and PNG generation. Retain its exact source, image, run/attempt,
firewall counters and cleanup evidence. Unit fakes do not prove these native
properties. The probe uses synthetic data and grants no SQL Explorer visual
approval. Protected exact-head review/approval and authorized main activation
remain separate requirements; scene geometry and every full-PNG verdict remain
mandatory for SQL Explorer repairs.

Native probe failure artifacts retain per-phase observations even when an import,
install, network check or rendering assertion fails. Capture records its actual
renderer versions and invocation, full font inventory and inspection results,
exact SVG, produced PNG and geometry/color observations before asserting glyph
presence. The phase record preserves the original exception and traceback;
unexecuted stages remain incomplete. Default renderer options and the nonempty
glyph assertion stay unchanged, so diagnostic retention does not imply native
acceptance or establish a font-selection cause.

Available probe files are collected after failed runs only when cleanup succeeded,
all owned containers are absent, no resource mutation is uncertain and upload
writers are safe. Missing or unsafe evidence remains a failed/incomplete result.
Owned firewall counters are sampled before cleanup even after a phase failure;
counter failures are recorded separately from the original error.

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

Rejection records what changed, why it was rejected and the required corrections.
The companion suppresses routine rejection, retry and maintenance Telegram chatter.
A fresh Pi correction agent plans the correction, a separate agent reviews that plan,
and another agent edits. Fresh independent PR review follows every changed head.
This cycle continues until approved, without an arbitrary retry cap or an owner
resume for routine rejection. Genuine questions wait for authenticated owner answers;
owner vetoes/cancellation and invalid provenance stop work. Authenticated feature
answers retain unrelated private pauses, including their reason, head and timestamp;
a factual reply cannot turn a provenance or cancellation hold into a recoverable
replanning pause. An unheld feature receives the normal replanning pause. A newer
amendment refreshes an existing replanning pause's head and timestamp atomically,
so an intervening resume comment cannot clear the newer planning requirement.
When an unrelated pause already exists, a separate durable amendment record retains
that newer requirement without changing the original pause. After an authenticated
resume or verified recovery clears the unrelated pause, the next pause check
atomically transfers the amendment into the normal renewed-planning hold. A resume
that predates the amendment cannot release it; discovery and merge remain blocked.
Policy-comment recovery compares the complete inspected pause/veto snapshot and
retains dev's authority protections. Successful retirement rechecks for a pending
amendment before reporting the PR unpaused. A failed snapshot comparison ends that
attempt: merge must not invoke label reconciliation to retry the same comment
against refreshed rows or fall back to a private approval.
The ordinary feature path still requires fresh planning, independent review and
coding before retiring its derived hold. Linked publication keeps that hold active
through checkout, archive writes and push. A host-only publisher argument binds to
the exact private pause (including its timestamp); it permits only the enrolled
feature's publication, never discovery, review, merge or policy approval. Intake
persists this binding before the first attempt and retires only that same row
after verifying the open PR's signed successor, task digest and producer. Failures
before push and lost responses after accepted pushes retain the hold and artifact
for restart. Replacement holds survive; legacy lost responses without a captured
binding cannot retrospectively authorize a pause. Deploy the compatible trusted
publisher before activating this companion path; an older publisher fails closed.
Verified publication recovery also compares the inspected pause's head, reason and
creation timestamp in its atomic deletion. It never calls the broad owner-resume
operation: original-evidence, cancellation and newer same-reason holds installed
between inspection and deletion survive recovery and restart. Correction-question
recovery likewise uses the base's shared atomic retirement operation: it deletes
only the full inspected row in the absence of a current-head veto, leaving labels
to normal reconciliation. A coordinator workspace receipt cannot erase
a concurrent cancellation or original-evidence requirement. Reconciliation owns labels.
Coordinator dependency waits also persist a separate controller hold before intake
releases worker ownership. It binds the request revision, complete task/question state,
checkpoint bytes, stage and dev base. Green checks, owner resumes, protected decisions,
amendment publication capabilities and derived-pause recovery cannot bypass it.
Only the matching supervisor-verified dependency receipt can transfer that exact
binding into the durable correction queue in one controller transaction. Merge
checks that queue even when earlier review/CI are green and no worker is available.
Fresh planning and independent review can proceed; failed publication leaves the
old head gated, and a signed successor still needs final review/CI. Existing
findings, concurrent replacement bindings and other requests remain held. Verification
does not retire unrelated pauses, grant approvals or replace fresh planning/review.
Legacy queued waits install the same gate during reconciliation. Activate compatible
protected controller support before this companion path; older controllers retain
a blocking compatibility pause rather than silently leaving a dependency ungated.
Explicit cancellation retains its stop-and-close behavior. Coordinator evidence
cannot replace the independent authority needed to resolve an existing hold.
Invalid task context also fails closed. The exact host-owned task-context mismatch
notice directs the coordinator to restore and verify matching authenticated context.
It requests no new owner answer or resume command, and retains the private provenance
pause. The additive `coordinator_blocked` field lets the compatible renderer show
awaiting coordinator verification without claiming corrections were queued.
Missing or false values retain legacy presentation. Rendering grants no delivery,
recovery or approval authority. Genuine missing owner input retains its authenticated
answer path. Cancellation and veto are owner decisions that stop work, not unanswered
questions; neither coordinator guidance nor an unrelated factual answer resumes them.
This notice change does not settle broader public label mapping or PR discovery.

Older trusted App repairs can inherit the standing integration-repair task, bound to their
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

The delivered companion admits up to ten Codex jobs concurrently. A PR has one writer and merges are
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

Companion PR #15 merged as `d0a34f3512de338d6d641568e9a95e1a7a863ca1`.
The supplied runtime pin is
[self_host `bfb4ba70ff85369292d347ab60a840d9bed133f6`](https://github.com/Karapsin/self_host/commit/bfb4ba70ff85369292d347ab60a840d9bed133f6),
indexed by the private immutable source and activation receipt
`operator/worker-replay-replan-20261017-v1-active.json`.
Its history retains the `05d5b826ae2e4879ba50debbd7f9ab1c3c48dd25` delivery
repair, clause routing, compatible renderer and prompt paging. The historical
`4e0db77e720d4df36dd1d0ed60a432402f266c0e` activation established rendering/paging
only; use the current source map and exact source paths rather than an obsolete
whole-source snapshot. Preserve subsequent runtime changes, all companion features
and private task records.

The supplied companion `test_current_notification_delivery.py` exercises the
actual producer through SQLite outbox, trusted eligibility, renderer and transport.
Its immutable `agent_tools/tests/fixtures/pr29_notification_producer.json` pins ATK
head `7e9770dbc1abc8ca4a2a3d1df5efd438ca9521d7` and controller SHA256
`6ea78c044de96ae3442e84feac92d9a0932dc5929a47c0bde0e196500c575156`.
That older producer does not validate this controller reconciled with current dev.
The orchestrator must refresh the pairing against the final immutable ATK commit
and module hash, retaining historical fixtures and rechecking the current authority
behavior. Existing source, CI and activation receipts do not clear that final-pair
dependency or any unrelated hold.

Controller pause, routine rejection and policy-approval producers add
`notification_category` (`paused`, `coordinator_blocked`, `review_rejection` or
`owner_approval`) and `request_origin` to the existing serialized payload.
`request_origin.branch` is the exact PR branch, or null when unavailable;
`request_origin.task_digest` is the event's task digest when available, otherwise
null. A pause does not borrow the current enrollment digest: its task context
may be the very binding in dispute. Existing repository, PR number and head
fields identify the event. These fields are lookup hints, not authenticated
Telegram origin, an owner-question classification, or permission to deliver.
In particular, `paused` includes cancellation and security holds; it does not
declare that an owner question exists.

The companion must resolve request identity and revision from trusted host
records, verifying repository, branch, available task digest and PR/head bindings against
the authenticated request. Branch prefixes, payload assertions and model output
cannot prove Telegram origin. A null or absent optional task-digest hint may derive only from
a matching authenticated private binding; concrete conflicting or malformed hints
remain unresolved. A coordinator hold for a verified Telegram feature
may qualify as feature status while retaining coordinator wording and all holds.
Legacy queued events must be reconciled against that trusted state before
rendering, stale-event retry handling or network calls. Missing or conflicting
bindings remain durably held for coordinator reconciliation, never delivered by
default. Confirmed suppressed maintenance events retain their original event and
deduplication key, with an atomic terminal audit disposition and reason, no
delivery receipt, and no retries across restart or duplicate enqueue. Eligible
events retain durable chunk progress, retry and deduplication.

ATK source preparation is permitted after independent plan review. ATK merge,
controller activation and paired-repair completion remain blocked until the
orchestrator verifies the final reconciled producer against the complete companion contract:

- Enforce outbound eligibility separately in companion `telegram.py`: permit owner
  approvals/questions, Telegram-requested feature implementation status, merges
  with dev/main and releases; suppress coordinator-only maintenance alerts.
- Preserve durable audit records and eligible-event retry, restart and deduplication.
  Suppression must be terminal, with no Telegram calls or recurring retries.
- Test the actual controller producer through serialized outbox, renderer and
  delivery for maintenance and Telegram-feature branches using mocked transport.
  Bind producer fixtures to an immutable controller commit and content hash and
  verify that they match the paired ATK implementation. Renderer tests alone prove
  neither outbound eligibility nor delivery or activation.
- Correct companion tests/docs that prescribe misleading public labels. Preserve
  provenance holds, cancellation/veto, genuine owner questions and exact-head gates.
- Route verified workspace-capability and coordinator evidence-access deficits
  through the existing question-policy/intake/native-planning path to coordinator
  tooling work. Retain every original question verbatim and record routing for each
  question and factual clause, including mixed clauses within a single string.
  Preserve the full task, checkpoints and genuine owner questions in mixed batches.
  Unknown factual clauses stay unresolved. Resume tooling-blocked work only after
  coordinator verification of repaired access, preserving all unrelated holds.
  Credentials, owner facts, original evidence, veto/cancellation and protected
  approvals retain their authenticated handling; routing cannot supply answers,
  approve a plan, fabricate access or bypass independent recommendation review.
- Obtain fresh independent source review, successful exact-commit Repository Checks,
  isolated rollout rehearsal and verified activation of the complete contract,
  recording deployed revision/file hashes and preservation of private state.

Final-head ATK `fast-checks`, fresh independent review and authenticated owner
exact-head protected approval remain required. Historical activation and older producer regressions
are insufficient to clear the refreshed final-pair dependency. Broader truthful label mapping and
complete PR discovery remain separately tracked unresolved work.

The merged companion includes the backward-compatible task-context reader from
PR #15. Legacy signed tasks remain valid; new plan/image evidence uses its own
signed digest, and questions remain visible beside owner answers.
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

Companion trigger discovery was delivered separately in self_host commit
`df672554b3c7eb7163f4249c9095e96b98ab04dd`, with immutable source and tests in
`operator/self-host-coordinator-df672554-complete-source-000.json`. The
parent-objective evidence retains its independent review and successful
[exact-commit Repository Checks](https://github.com/Karapsin/self_host/actions/runs/38991892068).
Its daemon-tick regressions cover repository-qualified inventories, human-created
PRs, rename origins, shared-file impact, comparison changes, incomplete evidence
and singleton/cadence behavior. The later `a6e713dd` activation receipt records
this delivered runtime chain separately from the `bde64ba6` fixture-only refresh.
These records replace the earlier missing-source dependency; they do not activate
this PR's protected controller support or establish all-relevant-PR completion.
Operational acceptance still requires fresh deployed-revision evidence and the
isolated rollback/recovery rehearsal through the guarded activation workflow.

Protected controller activation still requires successful final-head checks,
fresh independent review and the authenticated owner's exact-head
`/agent approve-policy <sha>` decision. Models cannot post that decision. After
initial activation, the idle trusted supervisor projects only allowlisted
newly merged automation paths to main. It reconstructs the merge tree from the approved head and the actual previous
dev, validates the recorded squash/merge tree and current-dev ancestry, and rechecks owner authorization
and signed task binding immediately before a non-force main push, and retains
failed activation for retry. Independently changed main files block projection.
The exact infrastructure path `agent_tools/sql_explorer_visual_render.py` also
belongs to this protected allowlist so the native capture probe can use the same
reviewed rendering policy on main. Its edits and either endpoint of a rename
require the same exact-head owner decision. Other `agent_tools` paths and SQL
Explorer application files gain no promotion permission. Activate the path policy
before processing a capture PR that needs the helper: the activation ledger does
not replay completed PR identities. If an older activation omitted the helper,
retain its ledger and require a separately reviewed, authenticated projection
with the existing merge, source, authority and main comparison checks.
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
intervenes only on demonstrated malfunction, and stops only after verified successful
merges drain all qualifying analytics_toolkit PRs, self_host PRs affecting GitHub
agents, and resulting repairs. Closed, abandoned, published or green PRs do not
satisfy completion. The thirty-minute singleton scheduler remains enabled for
future qualifying PRs; an orchestrator exits immediately when it finds no fix.

### Paired source and activation evidence

The retained [parent-objective evidence](../agent_tools/task_evidence/29adce308110491580e26a80f16fbb4a/parent-objective.json)
binds the complete companion source chain, independent reports and exact-commit
Repository Checks through `a6e713ddf02c176babde546a69b6b954c3d0df36`.
The coordinator's immutable manifest binds the ordered UTF-8 source bundles and
`operator/worker-retained-gate-a6e713ddf02c-active.json` (SHA256
`6f0b944c4072b94146d53fd50273892910752a1eb4a71c1f956ac137e6ef7024`).
That receipt supersedes the observed active `087a24fe0131` worker and the
historical, unactivated `ae8333b5` candidate. Its exact-commit successful
[Repository Checks](https://github.com/Karapsin/self_host/actions/runs/39000272860)
validate that worker revision only.

Source delivery, CI validation and runtime activation are distinct records.
The receipt explicitly reports that PR33 coordinator/renewal publisher APIs and
PR35 owner-attention APIs remain unavailable on its bound `ceff9e7a` controller.
This PR supplies the proposed PR33 controller support; it does not activate it or
claim completion of the parent monitoring objective. PR35 retains separate ownership.

The coordinator completed the companion fixture refresh in test-only commit
`bde64ba6c7026e37991397e59dda3da67eb550c5`. Its immutable index is
`operator/paired-fixtures-bde64ba6c702/index.json` (SHA256
`f47dc39b22d51ba317886814ebbe319fd943d74ef0ca8bfd3b5f2a479c132d10`).
The parent-objective record binds all five changed source hashes, the complete
independent review in that index's `review.json`, the successful full prepush
receipt and exact-commit
[Repository Checks run 39002462254](https://github.com/Karapsin/self_host/actions/runs/39002462254).

That historical producer/verified-renewal/restart/old-green-merge-refusal regression
loads `controller_recovery_contracts.json:coordinator_checkpoint_candidate`.
It binds checkpoint base `c125af22b47bff350686b5bd05ad226b7d307a8c`,
request `40ee14810aba` coding revision 8 / waiting revision 9, and predecessor
head `b822989acfd5bba65736ca55b4e5f7f9d1625f46`. Its controller SHA256 is
`081f2f38beb3a9fd5c9de23d08bc272b38f88de6445afabae65a52febb38eb0f`;
its review-control SHA256 is
`a675e4f6c9b21b9b7a8d869c2fd0fe63d52125350cce92440c8430445d8b1790`.
The final independent review binds the complete 56,891-byte checkpoint patch
(SHA256 `01532ed8e03d0232291a54a7b09318830591499be55b3f4b004220027a3cca65`),
superseding the earlier tracked-diff digest without changing those controller bytes.

Historical fixture sections and failure-before-push, accepted-push/lost-response,
legacy-binding, replacement-pause and producer/head regressions remain retained.
This refresh validates the retained source checkpoint and caller compatibility;
it requires no runtime deployment and does not change the observed active worker.
Fixture validation does not depend on activating the APIs it validates. Any later
controller source change requires refreshed immutable bindings, independent
companion review and exact-commit Repository Checks.

The coordinator subsequently delivered combined-source validation in self_host
commit `d0f6d916c597052c596fdf17f7371ee302ca910f`. Its immutable index is
`operator/pr33-combined-d0f6d916c597/index.json` (SHA256
`235500d7342253137b4935888e965fb58df794e1bdccea06d432c76f5e7893d9`).
The parent-objective record binds all three companion source hashes, independent
review, published-source verification and successful exact-commit
[Repository Checks run 39008919260](https://github.com/Karapsin/self_host/actions/runs/39008919260).
This supersedes `bde64ba6` as validation for the combined controller; the earlier
checkpoint records remain historical.

The combined fixture `agent_tools/tests/fixtures/coordinator_combined_code7.json`
binds controller SHA256
`f80b33ac82a8e590bba98822e79cc9d1a25816bddcd98343ea631f8607208fb1`
and review-control SHA256
`2016e391ea758606ae1ae9eb63319168c69f57724dff6edddf66c32ebc02741c`.
Its checkpoint identifies request `f7dc4258460e`, coding revision 7,
publication revision 8, input head `055f2383` and target dev `98e4236a`.
Both source hashes also match the later observed PR33 head `60fec897`.
The combined checkpoint tests exercise the real producer, dependency verification,
durable renewal, restart without a claimed worker and earlier-green-head merge
refusal, plus legacy-controller denial and the actual worker's internal/mixed
question attention caller. They exercise the existing attention APIs and fallback,
not PR36's separately owned `status_transition` API. This fixture-only commit
does not change the observed `a6e713dd` runtime or activate proposed support.

Complete logs for toolkit head `60fec897`, run `39008903001`, diagnose the same
owner-attention regression assertion in every Python job and quality coverage;
lint and type passed. A failed policy-comment recovery intentionally ends the
merge attempt before label reconciliation, retaining its existing labels. The
service-free regression now distinguishes that path from an existing private
approval, which permits hiding an internal display label while its pause still
refuses merge. Both cases retain the exact pause and approval records. Dedicated
later reconciliation can update display state; a hidden hint never authorizes
recovery retries or a fallback after a failed authority comparison.

Final toolkit review, exact-successor fast CI, real owner exact-head protected
approval, compatible controller activation and verified operational acceptance
remain separate gates. The coordinator provisions this evidence; the owner is not
asked to retrieve source files or CI logs.

The subsequent reconciliation targets dev
`581d81b6f18950b91164eabe6dfa32500db106f4`. It retains exact-pause resume,
pending amendments, dependency/renewal gates and dev's shared status transitions.
A resume whose inspected pause was replaced ends that attempt; it cannot retry
the same comment against a refreshed pause. After successful retirement, pending
amendments are checked before reporting unpaused. Amendment recording preserves
unrelated hold audit fields and vetoes while invalidating stale attention hints.
Both producers use read-only attention display updates without policy recovery.

The reconciled source hashes are controller
`1a6edae97f531a08decab6c30c2bbef1db5d6fa295ea88c002440eb2063bb234`
and review-control
`ad016e0df9aac0424f6d6a85577d2d89d4effcc3d7e7879300cdc769b1e1a782`.
The `d0f6d916` pair above remains historical validation for its recorded bytes;
it does not validate this reconciliation. Before protected approval, the
coordinator must supply refreshed immutable companion fixtures and selectors,
independent review and successful exact-companion-commit Repository Checks.
That refresh must exercise producer → verified renewal → restart → refusal to
merge an earlier green head, plus the actual worker caller through the shared
status-transition API. Source delivery does not establish deployment or successful
dev merges of all relevant PRs. Local programs and tests were not run; GitHub
must validate the final successor.

The follow-up for head `05acf2d776c1a7aacaf2ca89ce9330063abb3694`
keeps those controller bytes unchanged. Exact-head check records from run
`39014018021` report twelve test-setup failures: the discovery authorization fake
rejects `recover`, and status scenarios recreate production-initialized tables.
The correction accepts the keyword-only argument, inserts scenario rows into the
existing schema, and replaces only the test-local coordinator table for the
malformed-schema case. Restart, authority-preservation and WAL single-snapshot
assertions remain intact. That correction removed the duplicate recovery release
note in its checkpoint, but metadata publication at `4d2b1637` appended it again.
The replay/publication correction below removes that duplicate and makes exact
Unreleased entry folding idempotent.

The independently reviewed plan is
`8cc97542e01126e9d41893ab4c5f91d7c42aad765eb8d502fd7e0f8cfff7d47c`,
bound to the same pinned dev and head. This is source correction provenance,
not successful successor validation. The supplied companion evidence still lacks
a refreshed pair for these controller bytes; the coordinator owns that refresh
and its immutable source, independent review and exact-commit CI records.
GitHub must run the twelve focused cases and required fast checks on the final
successor. Protected approval, activation and all-relevant-PR merge acceptance
remain pending separate gates.

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

Controlled lifecycle-label producers share the display-only `status_transition`
boundary: attention creation/recovery, phase reconciliation and writer
acquisition/release remove conflicting statuses before appending the current one.
Per-PR host locks serialize controlled writers without a global worker lock.
Each mutation rechecks the open/closed state, exact head, authoritative issue
labels and private attention. A changed head aborts the old decision; a push by
itself never creates `needs-user`. Ordinary renewed planning keeps its merge
hold and displays its current phase without inventing an owner question.
Writer release removes only writing, preserving a later non-writing phase.
Closed cleanup preserves live Futures and stops on reopening. Visible owner
attention suppresses a phase even while its existing Future finishes; without
attention a live or unknown job retains its phase.

Readiness observation uses the pure authorization projection; authenticated
comment recovery remains an explicit separate workflow. Display updates do not
grant approval, clear a veto, answer questions or release a Future. Persistent
API errors and bounded lock/convergence failures retry through normal
reconciliation. GitHub does not provide an atomic compare-and-swap across label
requests, so the periodic reconciler remains recovery for interrupted operations
and external writers. Paired companion source/tests and exact-commit checks must
cover the final controller bytes before activation.

An authenticated amendment may require a new plan while its factual prerequisite
is still unmet. `readonly_coordinator_scope` permits `plan`, `plan-review` and
the separate read-only recommended-option `plan-resolver` under the exact retained request, published head and task binding. The dependency
hold stays in place. A second coordinator hold, owner pause, veto or pending
feature amendment denies that scope. The predicate processes no resume commands
and grants no coding, publication, policy approval or merge authority.
`replace_coordinator` atomically exchanges a retained inventory for its separately
reviewed successor, requiring the exact previous binding and a later revision.
Retries cannot reinstall a retired hold. The host must preserve authenticated
questions and answers and independently reassess the full amended scope; a new
plan alone never proves a prerequisite PR merged.

`prepare_coordinator_readonly` prepares only redacted review context for that
exact retained scope. Ordinary `prepare` still rejects a held PR. Preparation
checks signed task identity, current dev and private authority before and after
collecting evidence; the historical hold base is preserved. The paired worker
uses its bounded `Gate.observe` path and checks the full request and authority
again before launching the model. A controller without this capability produces
an internal tooling retry, with no owner question or unrestricted fallback.
Source publication alone does not deploy this capability.

Discovery revalidates an inventory PR after refresh and before publishing an
authorization or context rejection. A closed or changed target is discarded;
context rejection also rechecks before installing a pause. Real current open
authorization failures remain visible. GitHub state reads and check creation
are separate requests, so this closes the observed stale-inventory window
without claiming an atomic operation or rewriting historical check results.

The additive `prerequisite_authority_snapshot` and `renew_coordinator_checked`
APIs support callers that need a complete, receipt-selected control observation.
The full version-2 prerequisite review receipt commits every selected peer and
branch; its digest must match the persisted own hold. The snapshot retains all
selected rows, including historical rows and explicit absence, from holds,
renewals, pauses, vetoes, approvals, amendments, corrections and both feature
enrollment tables. Raw SQLite types and JSON text are preserved. Unsupported
schema, triggers, excess data, busy writers and expired metadata budgets stay
operationally pending. These APIs never create or migrate the database; normal
trusted `connect` performs only the additive checked-receipt table migration.

Checked renewal compares the entire observation in one `BEGIN IMMEDIATE`
transaction, preserves the own correction that must return to planning, rejects
active peer barriers, and changes only the exact own hold and renewal. It appends
an immutable checked receipt and verifies both receipt and authority after the
last write. A lost response can replay only identical inputs against the exact
unchanged post-state. A replacement or reinstalled hold is preserved and makes
replay stale. Existing ordinary renewal behavior remains separate.

The trusted consumer must keep its actual `SupervisorLock.assert_current_owner()`
ownership around the local call, bind full request and external proof identities,
and recheck them before advancing fresh planning. This is control-SQLite
atomicity; GitHub, request/jobs databases, source files and settings require their
own current observations and CAS. A checked receipt grants no coding, dispatch,
publication, protected approval or merge permission. Missing checked capability
has no legacy fallback for a composite capture prerequisite.

Source publication alone does not activate this API or its private schema.
Installed legacy workers admit exact whole controller trees: a new controller
closure needs a genuinely supported, independently reviewed source-admission
path before main projection. A protocol marker, compatibility JSON, healthy
heartbeat or earlier approval cannot supply it. The positive capture verifier,
guarded receiver, actual native/visual evidence, controller projection and worker
deployment remain separate requirements.

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
separate from the delivered companion trigger discovery and the remaining
protected controller activation. Deploy compatible reviewed controller readers
with separate isolated rehearsal; protected controller
activation still requires the owner's exact-head approval. Neither resolver
decisions nor companion recovery evidence releases PR #25's provenance hold.

`agent:needs-user` reports actual owner attention rather than every private merge
hold. Coordinator dependencies and authenticated scope amendments awaiting renewed
planning keep all authority gates while displaying their work phase. The host may
classify an exact derived pause for display only after inspecting all current
request states. Display hints cannot clear pauses, vetoes, coordinator gates or
approvals; replacement pauses and new genuine questions invalidate old hints.
Unknown/security holds and current-head policy decisions retain owner attention.

The companion chooses phases from live ownership before durable waiting requests
and current-head CI. An owner-attention label masks every phase while preserving
private job ownership. Other labels are retained.

| Label | Display condition |
| --- | --- |
| `agent:planning` | Live request validation or planning |
| `agent:reviewing` | Live independent review, plan review or plan resolution |
| `agent:writing` | Live code/publication or another recognized writer |
| `agent:queued` | Internal waiting/queued requests or an idle candidate awaiting work |
| `agent:testing` | Current GitHub fast-check aggregate or prerequisite jobs are unfinished |
| `agent:ready` | Current candidate satisfies the existing readiness gates |

Closed-PR cleanup removes obsolete phases only after live ownership ends, unless
owner attention already masks that phase. Every mutation rechecks PR state and
head; reopening returns the PR to normal reconciliation. API failures retry on a
later pass. The companion `test_agent_labels.py` and `test_closed_labels.py`
cover these transitions, CI, concurrent holds, failures and reopening; refreshed
`test_owner_attention.py` covers the production classifier with actual SQLite
requests and the paired controller candidate. These display checks grant no
merge or protected approval authority.

Both `paused()` and `authorization_error()` snapshot the complete PR pause and
current-head veto, including absent records, before looking up owner policy
comments. One shared helper compares that exact snapshot under `BEGIN IMMEDIATE`
and clears only unchanged owner-disapproval authority when the exact-head approval
is strictly newer than every observed decision. A later disapproval, veto,
evidence request, cancellation, or replacement hold survives an older lookup.
Changed snapshots block that attempt without retry or fallback to private approval,
so merge authorization cannot bypass the pause guard. Evidence and cancellation
holds remain intact. GitHub calls stay outside the transaction, and displayed
labels are repaired by normal reconciliation. Authenticated Telegram callbacks
retain their separate owner-decision clearing contract.

The production companion label caller is self_host
`8050fee15a67abc9f77450b181bea7d3304ed657`, retained in verified active worker
`a6e713ddf02c176babde546a69b6b954c3d0df36`. It classifies durable requests,
submits an exact-pause attention hint, reconciles owner attention, and then selects
one exclusive phase. Older controllers retain conservative attention labels.
The immutable evidence index `operator/paired-fixtures-bde64ba6c702/index.json`
(SHA256 `f47dc39b22d51ba317886814ebbe319fd943d74ef0ca8bfd3b5f2a479c132d10`)
binds refreshed actual-classifier/SQLite and hold-recovery tests to retained PR #35
code6 and PR #33 code8 sources. Companion commit
`bde64ba6c7026e37991397e59dda3da67eb550c5` passed
[Repository Checks 39002462254](https://github.com/Karapsin/self_host/actions/runs/39002462254).
Exact source, review, CI and activation references are retained in
[the task evidence](../agent_tools/task_evidence/df10e10053314309b74455688916a621/parent-objective.json).
This fixture-only refresh does not activate protected controller APIs. Final-head
review, GitHub checks, exact-head owner policy approval, controlled activation and
live label verification remain required. Changed bound controller bytes require
refreshed paired evidence before acceptance; all relevant PRs and repairs still
require successful dev merge.

### Combined controller reconciliation against dev 98e4236

The subsequent recorded resolution selected “Combine and revalidate” after
independent plan-resolver review. PR #33 now preserves the coordinator and
amendment gates together with the owner-attention APIs from pinned dev
`98e4236a0431d9f94bd322691eb7c8b9de204d22`. The separately owned PR #35
provenance and both historical implementations' evidence remain retained.
The earlier source, validation and activation descriptions above are checkpoint
records; they do not validate or activate this combined controller.

Amendment recording invalidates display hints in the same transaction that
preserves unrelated pauses and records a pending amendment. Displaying an
internal dependency without an owner-attention label still blocks a green merge.
Focused SQLite/fake-GitHub regressions cover both compositions, restart and veto
preservation. Policy-recovery scenario helpers retain every caller/change case
and assertion without increasing the lint baseline.

The combined source hashes and plan-review receipt are appended in
[the parent-objective evidence](../agent_tools/task_evidence/29adce308110491580e26a80f16fbb4a/parent-objective.json).
The coordinator must refresh both companion controller fixtures and selectors to
these bytes, including producer → verified renewal → restart → merge refusal and
the actual owner-attention caller. The `bde64ba6` index and successful Repository
Checks 39002462254 remain historical checkpoint evidence only. Refreshed immutable
source bindings, independent companion review and exact-commit Repository Checks
are still required before protected approval.

This private correction worker executes no programs or checks. GitHub validates
the final successor with lint, type, coverage, artifact and compatibility gates,
including `fast-checks`. Fresh implementation review, real owner exact-head
policy approval, guarded activation and deployed-revision evidence remain
required. Operator monitoring ends only after successful dev merges drain every
qualifying PR and repair; the scheduler remains available for future work.

### Exact coordinator replay and publication recovery

The reviewed correction for starting head `4d2b1637c10f20813d9c82f94b98a3c14e2e0209`
and pinned dev `581d81b6f18950b91164eabe6dfa32500db106f4` changes both controller
sources. A dependency installation consults retained renewals inside its transaction:
an exact already-renewed binding cannot reinstall its hold after restart or intake
advancement. Equal revisions with different complete bindings remain dependencies.
A renewal retry succeeds only for the retained repository, PR, request, complete
binding, head and digest, while the PR is open and has no current-head veto.
It retires only a matching hold, preserving replacement and unrelated authority.
The durable renewal continues to block an earlier green head.

Changelog folding consumes all validated fragments and appends each distinct entry
once. An exact complete single-line entry already under Unreleased is represented;
wrapped entries and historical-release text do not suppress a new entry. No-fragment
folding remains a no-op. The recovery bullet is retained once; the new task fragment
describes replay and folding repairs rather than recreating that folded note.
GitHub must verify the final metadata publication still contains exactly one copy.

The historical pair is now explicitly available in the immutable source chain:
`coordinator_shared_code6.json` and `test_coordinator_shared_checkpoint.py` from
self_host `3c906ec350634316545e4e7229417e09ef9a490c`, with independent review
`a82850c4d33aafdf410055d1e86eb628367ac8bb728437b3bbe891b2ba715d2a` and successful
[Repository Checks 39013892714](https://github.com/Karapsin/self_host/actions/runs/39013892714).
That pair covers the starting `1a6edae9` / `ad016e0d` controller sources.
The older combined fixture/module and `d0f6d916` receipts remain historical.
The pure shared-status caller in self_host `c211cc87` has separate source review
and successful [Repository Checks 39016268860](https://github.com/Karapsin/self_host/actions/runs/39016268860).

The supplied activation receipt
`operator/worker-feedback-lock-retry-20261017-v1-active.json`
(SHA256 `d3bdaa40c84cc616b44857ff4bf6b5130ba6bde1426e6a907f5a2cd8e89d4a16`)
retains these complete sources, reviews and CI records. It records worker
`f21c812a7292097892ec17761922d695b0b8e625` active with trusted controller
`13f0043b89d805ac5267d25fc0f4fdec7250f083`; it does not activate PR #33.

This patch's exact source hashes and historical evidence paths are appended to
[the parent-objective evidence](../agent_tools/task_evidence/29adce308110491580e26a80f16fbb4a/parent-objective.json).
The refresh-required record at this checkpoint remains historical. The following
successor delivery supplies its paired validation; final publication still needs
signed-context and fresh reviewer delivery verification.

### Successor paired evidence delivery

For published head `3a8f1dc5072c4638df02c2da600168251e522c00`, the assigned immutable
successor sources now contain self_host `bfb4ba70ff85369292d347ab60a840d9bed133f6`,
`coordinator_replay_code6.json` and `test_coordinator_replay_checkpoint.py`.
The complete embedded controller sources equal the retained controller bytes:
`4ad12163c313fa3d8a05682081954ebc03ddb01052a65765cc908558572f5e4e` and
`d5ec1e6bcba4666eaa83171be5fbfd97ad16b297c348d98979c233bcf63be71f`.
The fixture binds request `2eac395a9e9b`, coding revision 6, observed publication
revision 7, the original task digest, checkpoint manifest and patch. Complete
bindings, source inventories, independent source/checkpoint review reports and
successful exact-commit [Repository Checks 39024553657](https://github.com/Karapsin/self_host/actions/runs/39024553657)
are appended as `delivery_evidence.successor_evidence_delivery` in
[the retained parent objective](../agent_tools/task_evidence/29adce308110491580e26a80f16fbb4a/parent-objective.json).
Earlier questions, answers, plans, receipts and the refresh-required record remain intact.

The companion regression uses actual intake to commit renewal and lose its response
before request advancement, restarts with fresh readers, and retries successfully.
It replays hold N after queued N+1 and asserts the dependency stays absent before
another renewal. Replacement bindings, head/digest mismatches, veto/provenance holds,
unchanged checkpoint/context and refusal to merge the earlier green head remain
covered. Inherited producer tests select the same pair and exercise the actual pure
shared status caller. The fast archival-evidence regression checks source, review,
request and CI consistency without executing archived companion code. Its source
manifest digest checks assert length and hexadecimal characters separately to
satisfy Ruff PT018 while retaining both checks. This test-only correction leaves
the paired controller bytes unchanged; GitHub must validate the final successor.

The assigned activation receipt
`operator/worker-replay-replan-20261017-v1-active.json`
(SHA256 `4c6a65479122638575d31c4546a7c3c459c533d592edbcc6798d9b9d2e1b6a25`)
records that companion worker active, preserving 107 checkpoints and the schedule.
The trusted controller remains `13f0043b89d805ac5267d25fc0f4fdec7250f083`;
PR #33's proposed controller pair has not been activated. Historical source-only
review flags in the receipt remain historical, rather than new activation claims.

Every required envelope and the complete source/review/CI receipt are present in
this author assignment. The coordinator must attach the appended bindings to the
signed successor context and verify precise source, test, review and CI delivery in
the actual fresh reviewer assignment after publication. This assignment does not
prove future delivery. Metadata publication changing controller bytes requires
another paired refresh. Starting-head fast CI succeeded; the final successor still
requires its own fast CI, complete independent implementation review, real owner
exact-head protected approval and guarded controller activation. Dependency holds
and the all-relevant-PR successful-dev-merge objective remain in force. No programs
or tests were executed by this private worker.

[Documentation overview](README.md)
