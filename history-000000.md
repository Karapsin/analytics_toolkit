[Task archive index](README.md)

<a id="task-message-0"></a>
## Starting user prompt

<pre>check RPI agents, their, especially those who waiting for user approval</pre>

<a id="task-message-1"></a>
## User correction 1

<pre>Implement the plan.</pre>

<a id="task-message-2"></a>
## User correction 2

<pre>also update rpi agents to ALWAYS use recommended options when they have questions about plan/feature implementation/correction</pre>

<a id="task-message-3"></a>
## User correction 3

<pre>and I want to get messages only about user approvals / tg requested feature implemntation status / pr mergres with dev / released / merges with main</pre>

<a id="task-message-4"></a>
## User correction 4

<pre>and once you done - monitor PRs until completion, stop only when ALL of them are merged successfully with dev</pre>

<a id="task-message-5"></a>
## User correction 5

<pre>fix any issues on the way</pre>

<a id="task-message-6"></a>
## User correction 6

<pre>also restart agents who are waiting user&#x27;s approval so that use this new policy</pre>

<a id="task-message-7"></a>
## User correction 7

<pre>also for tg questions, allow respond in github as well</pre>

<a id="task-message-8"></a>
## User correction 8

<pre>check any pending PRs for agents which wait for something and reposnd them after that</pre>

<a id="task-message-9"></a>
## User correction 9

<pre>ok, fix those tooling issues</pre>

<a id="task-message-10"></a>
## User correction 10

<pre>also I&#x27;ve noticed that user answers to agent&#x27;s question recorded and posted in PR, but agent&#x27;s question is not visible =&gt; not enough context, fix that as well, record both questions and answers</pre>

<a id="task-message-11"></a>
## User correction 11

<pre>also raise RPI agents concurrency to 10</pre>

<a id="task-message-12"></a>
## User correction 12

<pre>Questions: [&quot;1. Missing approved plan: no supplied record establishes what “Implement the plan” approved. May your answer explicitly replace that missing reference with the RPI requirements in your chronological messages, preserving the existing plan/image/question-answer evidence machinery but removing unsupported claims that you approved DataLens/PR #12 behavior?\nUse explicit replacement (Recommended): Authorize this RPI scope and retain the feature; no DataLens scope or historical approval is inferred.\nRecover original plan first: Keep planning blocked until the exact original plan and trustworthy approval provenance are supplied.&quot;, &quot;2. Monitoring scope: which PRs and repositories does “ALL ... merged successfully with dev” cover? The supplied snapshot identifies analytics_toolkit PR #15 and several self_host companion revisions, but provides no complete pending-PR inventory.\nEnrolled repos snapshot (Recommended): Inventory all open PRs in RPI-managed repositories at rollout, including drafts, plus repair PRs needed for their completion; retain each repository’s authorized target branch.\nanalytics_toolkit only: Monitor every currently open analytics_toolkit PR and resulting repairs until merged into dev.\nExplicit PR list: Provide repository/PR identifiers and target branches to define the completion set.&quot;, &quot;3. Tooling scope: does “fix those tooling issues” mean the failures represented in the supplied companion evidence—source/policy access, exact-head CI-log delivery, long job identifiers, claim cleanup, and restart/checkpoint recovery—or were additional specific failures discussed in the missing conversation?\nEvidence-listed failures (Recommended): Limit tooling corrections to those recorded failures and issues necessary to complete the selected PR set.\nAdditional failures exist: Provide the omitted failures or conversation so the plan can include them without inventing scope.&quot;]
User response: Use the recommended RPI coordination scope, including evidence-listed failures and issues necessary to complete the target PR set. The exact original native plan was recovered before replacing anything: operator/codex-plan-88852a9c0686faeb.json (SHA256 6b3e4b130a2ccdf769fa7ce0c89c51d272212d3374b1b86d7cff71f19d561e1e). It identifies the native plan item and the subsequent actual owner&#x27;s &quot;Implement the plan.&quot; message. The earlier attachment&#x27;s plan text was a paraphrase; correct that record by retaining its provenance and appending the exact recovered plan/approval binding rather than silently rewriting user history. The native plan explicitly confirms five DataLens behaviors as replacement scope for #12; it does not reconstruct the unseen image. The chronological RPI requirements in this task also authorize recommended choices, question/answer recording, GitHub replies, notifications restricted to approvals/Telegram-feature status/dev merges/main promotions/releases, ten workers, and fixing blockers until all target PRs merge.

Monitoring: use Enrolled repos snapshot (Recommended). The live worker is enrolled for Karapsin/analytics_toolkit, whose current complete open set is #7,#9,#10,#11,#12,#13,#14,#15, plus repairs needed to finish these. Preserve authorized dev target. Karapsin/self_host is the companion repository; its current open-PR inventory is empty and the supplied companion changes use its authorized direct-dev workflow. Do not infer authority to merge unrelated PRs in other repositories.

Tooling: evidence-listed source/policy access, exact-head CI-log delivery, long job keys, claim cleanup, restart/checkpoint recovery, legacy unlabeled routine questions and visual capture/rejection retry are completion blockers. Genuine missing evidence/credentials, vetoes, provenance and protected exact-head approval remain real holds. Resume unlabeled routine questions through fresh planning without inventing owner answers.

The fragment finding is a metadata-fold false positive: authored head 025349d5708c8a1eda8fa87df44687d99d1f1795 had the wrapper-owned fragment; its immediate child 11ce is a &quot;GitHub agent: metadata&quot; commit that removed this fragment and folded its exact bullet into CHANGELOG. Immutable trace: operator/pr-15-authored-fragment-fold.json. Honor legitimate author fragments followed by trusted folds; do not create an endless fragment recreation/fold loop. Protected policy approval for the old head is not approval for a corrected head.

The companion staged revision 82a44a54585efc641018c09c1bc8690e98a7dbe6 additionally carries typed visual rejection/replan and exact-head CI success; legacy-question recovery is being repaired with a new independently reviewable snapshot. Do not claim the old companion review approves the newer code. Exact recovered approved plan follows:

# Preserve approved plans and image evidence for agent review

## Summary

Resolve PR #12 using your confirmed DataLens scope, and extend task attachments so future reviewers receive the approved plan and referenced images.

## PR #12

- Record your explicit authorization to preserve copy-mode tab/dashboard imports, encoding and parameter fidelity, managed Editor link relocation, inferred dataset GUID recovery, and component-error diagnostics.
- Recover the original plan and image from available session records where possible. Record missing evidence honestly; defer comparison against the original image.
- Deliver this clarification through the authenticated owner reply workflow. Preserve Pi ownership of corrections, independent review, and required checks.
- Retain the correction plan for interrupted dataset recovery and the task-specific changelog fragment.

## Durable changes

- Introduce a versioned task-context format supporting exact assistant plans, chronological user approval messages linked to those plans, and image evidence records. Preserve existing version-1 parsing and digest verification without rewriting historical records.
- Extend `task_context` with actions to record plans and image evidence. Keep the original prompt immutable; distinguish assistant content from user authorization.
- Store private originals locally. Publish redacted plan text and image descriptions; place sanitized images in immutable reviewer evidence with content hashes. Require owner review of images before external publication.
- Include new evidence in the signed task digest. Supply it to planning, correction, and independent review sessions through the existing evidence interface.
- Before handoff, flag unresolved references such as “Implement the plan” or `[Image #1]`. Require attached evidence or an explicit user replacement of the missing requirement.
- Update local and Telegram workflows to retain plans and attachments before implementation. Deploy compatible readers before enabling new-format writers.
- Follow independent review, fast checks, and retained exact-head owner approval for protected controller changes.

## Validation

- Verify version-1 compatibility and version-2 recording, redaction, rendering, parsing, and signature binding.
- Reject altered plans, mismatched approval references, missing image files, and image hash mismatches.
- Verify chronological corrections, restart persistence, reviewer access, and explicit missing-evidence replacements.
- Reproduce PR #12’s original attachment gap and confirm the new workflow supplies sufficient context.
- Run focused and mandatory precommit checks; validate Pi companion changes on GitHub.

## Defaults

- Your confirmed five DataLens behaviors replace the unavailable original scope evidence for #12.
- Images include a written description and sanitized original when available.
- Missing evidence never becomes invented context, and scope confirmation does not waive technical review or checks.</pre>

<a id="task-message-13"></a>
## User correction 13

<pre>Questions: [&quot;The approved plan requires companion revision 82a44a54585efc641018c09c1bc8690e98a7dbe6 and the later legacy-question recovery revision, but the supplied immutable evidence stops at 1f89613228ce1a36287b779867f20ed8430193e0. Can the coordinator provide those exact source/test snapshots and a separate writable source_files assignment for the self_host correction? (Recommended.) Alternatively, explicitly limit this invocation to the toolkit provenance patch and leave companion implementation to its separate worker.&quot;]
User response: Coordinator-provided exact source/test snapshots are now available: self_host-task-evidence-82a44a54585e/ for the visual rejection/replan change and self_host-task-evidence-0ae2ffc5ce58/ for its descendant adding legacy unlabeled-question recovery. Both are hash-bound in the current manifest.json and have green exact-commit CI receipts. Your 4-code snapshot was exported before the final evidence arrived; a fresh invocation will receive all 155 declared files. Use list_evidence/read_evidence_file, starting with the new manifest.

The coordinator owns the separate writable self_host checkout and has already implemented, validated and published that companion work at 0ae2ffc5ce5892bedd4a37f627dc9b190958e27b; no duplicate companion implementation is needed in the toolkit source_files assignment. Your writable assignment remains the toolkit provenance/evidence patch and any toolkit findings within the approved plan. Inspect the pinned companion as immutable dependency evidence; preserve its separate independent review and staged-not-active deployment boundary. The complete task still includes companion rollout, worker recovery and all target PR merges. This is an allocation of the already authorized work between its existing owners, not a reduction of feature scope. Preserve your saved toolkit patch, replan against the latest evidence, and retain exact-head owner approval and all required checks. No owner is asked to provide source files or tools.</pre>

<a id="task-message-14"></a>
## User correction 14

<pre>Questions: [&quot;Can the coordinator make manifest.json readable through bounded source_files pages, preferably by formatting it across multiple lines? The current single-line response is truncated at 23,997 characters, and reading from line 2 returns nothing. This hides the pinned companion revision and exact-commit CI receipts required by the reviewed plan.&quot;]
User response: The coordinator has reproduced the single-line truncation and is fixing the exporter so both normal and fast-CI manifests retain numbered-line pagination. A small immutable receipt is already supplied at operator/companion-0ae2ffc5ce58-ci.json, containing the actual pinned 0ae2ffc5ce5892bedd4a37f627dc9b190958e27b source/test paths, hashes and successful exact-commit Repository Checks receipt. It is less than the reader limit and is formatted across lines. Read it directly with read_evidence_file; use list_evidence with prefix self_host-task-evidence-0ae2ffc5ce58/ to inspect the source and regressions. Do not retry reading the entire legacy single-line manifest or ask the owner for files. A fresh invocation receives this declared hash-bound receipt. The serialization repair belongs to the separate writable companion checkout and is being checked/published there; the final independent review must inspect its final validated companion pin. Preserve your toolkit patch and full approved scope, replan against these receipts, and retain independent review, fast CI and exact-head protected owner approval. CI evidence is not owner approval.</pre>

<a id="task-message-15"></a>
## User correction 15

<pre>/replace_reference {&quot;reference&quot;: &quot;Implement the plan&quot;, &quot;replacement&quot;: &quot;For PR #15, use the already recorded task scope: preserve the complete approved-plan, chronological question/answer and image-evidence feature, backward compatibility, immutable originals, redaction, provenance and publication safeguards. Retain the recovered plan titled Preserve approved plans and image evidence for agent review and its recorded historical approval binding at original correction index 0. Later quotations and descriptions of that historical instruction, including the legacy question/answer narrative in correction 11, refer to that same record; they are not additional plan approvals or missing new plans. This clarification supplies no fabricated historical approval. Apply the chronological RPI requirements: request-aligned resolution of routine recommended choices, authenticated GitHub answers, complete questions and answers, ten-worker concurrency, checkpoint/claim/restart recovery, source/policy/CI-diagnostic tooling repairs, the requested notification restrictions, and completion of the enrolled analytics_toolkit PR set and necessary repairs into dev. Preserve companion ownership and rollout obligations. For #12, retain the recorded textual scope of copy-mode tab/dashboard imports, encoding and parameter fidelity, managed Editor link relocation, inferred dataset GUID recovery, and component-error diagnostics. The unavailable original image remains unavailable; comparison against it stays deferred and its contents must not be invented. Preserve the actual PR #9 dev-merge dependency and every genuine evidence, credential, provenance, veto and private hold. Revalidate current evidence and obtain fresh independent plan and implementation review, exact-head CI and visual validation, required protected approval, isolated deployment rehearsal and live acceptance. This is an automated clarification under the user&#x27;s standing delegation, not a new human answer, current-candidate plan approval, protected-code approval or authorization to clear unrelated holds.&quot;}</pre>

## Agent question 1

<pre>Provide the original approved plan or original image evidence, or an explicit owner-approved replacement, before coding: Missing image evidence or explicit user replacement: [Image #1]</pre>

**User answer:** [Complete user correction 15](history-000000.md#task-message-15)

## Assistant evidence: plans

<pre>[
  {
    &quot;approval_index&quot;: 0,
    &quot;id&quot;: &quot;88852a9c0686faeb3176b615ed69e8e626963dbaa1f358eed5ac06b2001c1dae&quot;,
    &quot;text&quot;: &quot;# Preserve approved plans and image evidence for agent review\n\n## Summary\n\nResolve PR #12 using your confirmed DataLens scope, and extend task attachments so future reviewers receive the approved plan and referenced images.\n\n## PR #12\n\n- Record your explicit authorization to preserve copy-mode tab/dashboard imports, encoding and parameter fidelity, managed Editor link relocation, inferred dataset GUID recovery, and component-error diagnostics.\n- Recover the original plan and image from available session records where possible. Record missing evidence honestly; defer comparison against the original image.\n- Deliver this clarification through the authenticated owner reply workflow. Preserve Pi ownership of corrections, independent review, and required checks.\n- Retain the correction plan for interrupted dataset recovery and the task-specific changelog fragment.\n\n## Durable changes\n\n- Introduce a versioned task-context format supporting exact assistant plans, chronological user approval messages linked to those plans, and image evidence records. Preserve existing version-1 parsing and digest verification without rewriting historical records.\n- Extend `task_context` with actions to record plans and image evidence. Keep the original prompt immutable; distinguish assistant content from user authorization.\n- Store private originals locally. Publish redacted plan text and image descriptions; place sanitized images in immutable reviewer evidence with content hashes. Require owner review of images before external publication.\n- Include new evidence in the signed task digest. Supply it to planning, correction, and independent review sessions through the existing evidence interface.\n- Before handoff, flag unresolved references such as “Implement the plan” or `[Image #1]`. Require attached evidence or an explicit user replacement of the missing requirement.\n- Update local and Telegram workflows to retain plans and attachments before implementation. Deploy compatible readers before enabling new-format writers.\n- Follow independent review, fast checks, and retained exact-head owner approval for protected controller changes.\n\n## Validation\n\n- Verify version-1 compatibility and version-2 recording, redaction, rendering, parsing, and signature binding.\n- Reject altered plans, mismatched approval references, missing image files, and image hash mismatches.\n- Verify chronological corrections, restart persistence, reviewer access, and explicit missing-evidence replacements.\n- Reproduce PR #12’s original attachment gap and confirm the new workflow supplies sufficient context.\n- Run focused and mandatory precommit checks; validate Pi companion changes on GitHub.\n\n## Defaults\n\n- Your confirmed five DataLens behaviors replace the unavailable original scope evidence for #12.\n- Images include a written description and sanitized original when available.\n- Missing evidence never becomes invented context, and scope confirmation does not waive technical review or checks.\n&quot;
  },
  {
    &quot;approval_index&quot;: null,
    &quot;id&quot;: &quot;dad771f3699383231ae5981e127139e2c541341182b6d5d572b5ba0c96d00365&quot;,
    &quot;text&quot;: &quot;# Reconcile PR #15 with current dev and complete the approved RPI work\n\n## Summary\n\nPreserve the complete saved feature while reconciling it against dev `43edcf11550c28f42645c15a92e41b8f4340d0c1`. Only the approved coder resolves conflicts. This planning session makes no edits and runs no programs.\n\nRetain the full task-context v2, question/answer, image, provenance, recovery and coordination scope. Keep the actual PR #9 dependency and all genuine private holds until current evidence establishes their release conditions. Historical CI, visual captures and approvals do not validate the reconciled candidate.\n\n## Conflict resolution and compatibility\n\nResolve all twelve conflicted paths as follows:\n\n| Paths | Required resolution |\n|---|---|\n| `.github/agent/controller.py` | Combine saved reference/image validation and safe recovered-head reuse with dev’s archive publication and authority rechecks. Validate before publication writes. Create a signed commit only when a verified existing commit cannot be reused. Retain dev’s `merge` compatibility wrapper and `merge_selected` implementation, including active-branch protection and every merge gate. |\n| `.github/agent/review_control.py` | Combine notification filtering with dev’s bounded outbox processing. Preserve actionable owner questions and context-blocked notices; suppress routine local status/rejection noise without deleting payloads, delivery history or private holds. |\n| `.github/agent/task_context.py` | Retain dev’s archive reader, bounded compressed v2 transport, duplicate-marker rejection and existing reference-example handling. Preserve all saved evidence validation and signature binding. |\n| `agent_tools/task_context.py` | Retain both v2 recording/preflight/review-record functionality and dev’s repository-bound archive publication/parsing helpers. |\n| `agent_tools/sessions.py` | Run reference/image preflight and complete transport preflight before publication. Preserve dev’s post-archive session/head/signature rechecks and retry-safe handoff. |\n| `tests/agent_tools/task_context.py` | Retain both feature regressions and dev’s oversized-history/archive regression. Split cohesive test modules if needed to satisfy repository limits. |\n| `agent_tools/README.md`, `docs/GITHUB_AGENT_OPERATIONS.md` | Combine feature documentation with current archive, controller, lifecycle and rollout behavior. Remove obsolete active-pin and deployment claims while retaining clearly labelled historical evidence. |\n| `docs/CHANGELOG.md` | Preserve both sides’ Unreleased entries and all foreign/versioned history. Keep one existing `1.3.12.5 - 2026-10-14` heading; do not change package versions or recreate consumed fragments. |\n| `docs/INTEGRATION_TESTING.md`, `tests/sql/integration_harness/faults.py` | Preserve incoming restart/retry/restoration documentation and regressions, alongside saved MinIO guards and diagnostics. |\n| `release_routines/coverage_targets.json` | Retain dev’s stronger ratchets: branches `98.03`, combined `98.92`, statements `99.22`. Do not lower thresholds to accommodate the patch. |\n\nPreserve incoming orchestrator, enrollment, activation, protected-controller and backward-compatible reader changes outside these conflicts. Remove every conflict marker and pass the trusted collector’s whitespace/conflict checks.\n\n## Feature completion and targeted repairs\n\n- **Evidence and provenance:** retain exact plans, complete questions/options, chronological actual answers, private originals, redaction, image publication consent and content hashes. Preserve v1 signatures and existing v2 field spellings without historical normalization.\n- **Historical correction:** preserve the original published v1 record, saved v2 projection and native recovery proof. Publish an auditable amendment binding the recovered plan only to authenticated correction `0`. Correction `11` is historical citation, not another approval. Do not retain unsupported approval or image-replacement assertions merely because the checkpoint contains them.\n- **Reference resolution:** preserve the supplied automated clarification as automated evidence, not a human answer or protected approval. Resolve the documented historical quotations against their recovered-plan provenance without blanket quote exemptions. Genuine missing plan/image requirements must still block publication; the unavailable PR #12 image remains unavailable.\n- **Visual routing:** extend `visual_required` to cover the five evidenced harness/geometry paths: `tests/agent_tools/visual.py`, `_support/visual.py`, `_support/geometry.py`, `visual_diagnostics.py` and `geometry_reporting.py`. Retain existing production/manifest/capture detection, inspect both rename endpoints, and preserve task/patch/capture identity checks.\n- **Visual diagnostics:** retain complete geometry JSON and bounded failure summaries with always-uploaded artifacts. Preserve every scene assertion. The later successful capture does not establish the original failure’s cause; any new behavior repair requires cause-specific fresh planning and review.\n- **Quality diagnostics:** implement the resolver-selected durable-artifact repair in `release_routines/lib/quality_debt.py`, its focused tests and `.github/workflows/tests.yml`. Persist full stdout/stderr before validation or parsing, including malformed-output and tool-failure paths. Artifact indexes bind commands, exit codes, toolchain versions, output hashes, actual checkout revision and PR source head. Keep concise summaries, existing return contracts and debt enforcement unchanged; expose complete artifacts through coordinator evidence.\n- **MinIO:** retain the evidenced E501/FORMAT correction and service-free configuration guards without changing merged source pins, health checks, bucket initialization or cleanup behavior.\n\nNo public package API changes are required. Retain the approved additive task-context/MCP recording interfaces and all incoming transport interfaces.\n\n## Focused regression coverage and GitHub validation\n\nRun validation on GitHub or authorized isolated infrastructure; never execute candidate programs, tests or rendering on the private Pi.\n\n- Exercise real publication and recovery boundaries with mocked external writes: unresolved references, altered plans, removed supplements, invalid image blobs, stale ownership/head/body, archive retries and recovered-branch reuse must fail safely.\n- Cover v1, inline/compressed v2 and archived round trips; complete oversized questions/answers; starting-prompt bindings; authentic historical imports; and negative fabricated-approval/replacement cases.\n- Add service-free visual-routing tests for every new and existing path family, rename-in/rename-out and unrelated paths. Retain stale/partial/non-green receipt rejection.\n- Verify long Ruff/mypy diagnostics survive aggregation, parse failures and nonzero exits, with complete uploaded artifacts and unchanged gate results.\n- Retain notification backlog, multipart delivery, genuine-question, scoped-merge, incoming SQL fault and MinIO regressions.\n- Require final-head fast CI, lint/type/coverage/artifact gates and the supported Python/SDK matrix. Preserve current coverage and debt ratchets.\n- Obtain a fresh full visual capture of the reconciled candidate and review every manifest PNG individually—currently 46 scenes. Require all geometry checks and individual verdicts to pass before publication.\n\nFresh independent plan and implementation review must cover the complete reconciled diff, all conflict resolutions and the actual companion dependency. Obtain authentic protected approval for the final exact head after required checks pass.\n\n## Companion rollout and completion\n\nThe chronological `0c1f1abe…` receipt remains required historical lineage. The current manifest identifies newer active companion `ab7f83cddb6981ee380d3ae76fbc51ba1a41c87a`; preserve its separately owned changes and verify its complete source/activation receipts rather than rolling it back or duplicating implementation. Update rollout records and their regression together to distinguish historical, staged and evidenced-active revisions.\n\nRetain recommended-choice resolution through the separate alignment resolver, authenticated GitHub answers, ten workers, checkpoint/claim recovery, tooling retries, exclusive lifecycle labels, capacity cooldowns, unlocked visual review and scoped merges. Credentials, original-evidence gaps, vetoes, cancellation, provenance failures and protected decisions remain genuine holds.\n\nBefore any remaining activation, verify compatible approved trusted readers, independent review, final CI, exact-head owner approval and an ex</pre>

## Assistant evidence: plans (continued)

<pre>act-source isolated rehearsal covering recovery, genuine holds, notification delivery, writer/lock behavior and state-preserving rollback. Then perform controlled activation and live acceptance without discarding later answers, checkpoints or private decisions.\n\nThe coordinator must establish PR #9’s actual successful dev merge and ancestry before releasing its dependency hold. Root retains responsibility for analytics_toolkit PRs `#7,#9,#10,#11,#12,#13,#14,#15` and necessary repairs through verified successful dev merges, companion acceptance and required post-merge repair monitoring. No completion claim follows solely from PR #15 publication or staged policy changes.\n&quot;
  }
]</pre>

## Assistant evidence: replacements

<pre>[
  {
    &quot;reference&quot;: &quot;Implement the plan&quot;,
    &quot;user_index&quot;: 14
  }
]</pre>

## Assistant evidence: decisions

<pre>[
  {
    &quot;choice&quot;: &quot;quality_diagnostic_retention: Durable artifacts (Recommended)\nReason: Retaining complete diagnostics with source-head, command and toolchain binding supports the owner&#x27;s authorization to fix tooling and completion blockers, including exact-head CI-log delivery. Artifact references and long-output regression coverage address diagnostic loss while preserving concise summaries, existing return codes and debt enforcement. This leaf repair leaves the full companion rollout, worker recovery and all-target-PR monitoring objective intact; required independent review, GitHub validation and real-owner exact-head protected approval remain mandatory.&quot;,
    &quot;questions&quot;: [
      &quot;1. For the authorized quality-debt diagnostic repair, should the wrapper retain complete raw tool output in always-uploaded artifacts bound to the source head, command and toolchain, while keeping concise CI summaries and existing gate return codes?\nDurable artifacts (Recommended): Preserve full stdout/stderr before parsing or failure, expose artifact references to correction workers, and test long diagnostics without changing debt enforcement.&quot;
    ]
  }
]</pre>

