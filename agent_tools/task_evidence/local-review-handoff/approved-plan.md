# Approved workflow scope

Historical reference: this file at toolkit commit
`ff01efb7c5e8ae833ce4dc6e02f94a45f1457f7f` records the workflow redesign referenced
by the user's “Implement the plan” request. The chronological owner answers in
[comment 6364782365](https://github.com/Karapsin/analytics_toolkit/pull/53#issuecomment-6364782365)
and explicit replacement in
[comment 6367810837](https://github.com/Karapsin/analytics_toolkit/pull/53#issuecomment-6367810837)
amend that reference and override its conflicting cancellation and retry-limit text.

The original instruction to stop automations and concentrate on merging PR #9 was
not cancelled. Automations remain stopped pending separate activation. Completing
PR #9 remains an outstanding obligation deferred outside PR #53; it is not this
PR's acceptance criterion.

The user's stated changes were:

- Remove Pi feature requests and feature development through Telegram.
- Keep initial review/correction on the machine developing the feature, outside GitHub.
- Keep the orchestrator. Restrict the GitHub coding role to conflict resolution.
- Never return published tasks to the developing machine.
- Use the newest supported Python for dev checks to reduce turnaround time.
- Use separate Pi repair agents for post-merge failures, as with integration failures.

The owner amendments assign pre-merge CI failures to separate Pi repair agents
and changed heads or bases to independent Pi reviewers. Feature creation and initial
independent review/correction stay on the developing machine.

Full compatibility, minimum-dependency and SDK validation remain after merges and
during releases. Python 3.14 is the package's newest supported runtime; lint/type
tooling may retain its pinned interpreter. Coverage, artifacts, fast regressions,
independent review and SQL Explorer visual requirements remain.

Local publication requires a fresh read-only reviewer, separate from the author,
to approve the exact head, dev base and user-task digest. Rejected candidates stay
local for correction and another fresh review. The signed review receipt travels
with the PR and is verified against host-owned enrollment. Changed heads/bases
after publication receive fresh Pi review and any repair remains Pi-owned.

The orchestrator coordinates checks, provenance, one writer per PR and merging.
Separate repair agents retain diagnostic accounting, rejected-patch evidence,
incident/task bindings and deduplication across restarts, changed heads and partial
repairs. No new repair retry limit is introduced: three unsuccessful candidates
do not create a pause. Established repair controls and genuine holds remain.
Infrastructure/authentication failures remain operational retries or holds.
Authenticated owner controls remain required for resuming genuine paused incidents;
publication does not clear persisted pauses or reset private state.

Implementation spans this toolkit repository and the companion `Karapsin/self_host`
worker. Private, task-bound review evidence supplies the accepted companion commit,
complete source/test evidence, independent source review and exact-SHA CI.
The selected accepted companion is
`af6162e72107562d4bbeb0948f87a9d235ef92ca`, replacing the earlier verification
candidate `7cb3b91aabc500055db0cff6ea2b77f88ca2bac8`. The supplied independent
acceptance record `rollout/worker-af6162e72107/acceptance-review.json` has SHA256
`24660c6f473822e0dc75026dc06879f0806b8028cc83a85a930ea0e7184c771e` and accepts
that exact companion source, tests and documentation. Its source receipt,
`rollout/worker-af6162e72107/source-receipt.json`, has SHA256
`e29a2f9c69e2796b1506be3f887e3fd74172f3b5e5e0bf6fce55b651cd42a1a7` and binds
immutable source/test references in `source-00.json` through `source-03.json`
under the same evidence directory, together with successful exact-commit
[Repository Checks](https://github.com/Karapsin/self_host/actions/runs/39994631177).

For this post-handoff Pi correction, the coordinator-owned private binding is already
provisioned in the Pi review-evidence catalogue, as clarified by the owner in
[comment 6372220942](https://github.com/Karapsin/analytics_toolkit/pull/53#issuecomment-6372220942).
Catalogue source `worker-rollout-source-af6162e72107` binds the exact companion commit,
source receipt and Repository Checks run above; the acceptance record is available
at the path and SHA256 above. The developing-machine file
`.git/atk-local-review-evidence.json` serves local review before handoff only; this Pi
correction does not use it and requires no provisioning or changes to that file.
Carry the complete chronological task, including this clarification, through the
existing correction publication and review bindings. Bind the current task digest;
do not reuse a historical digest. Private companion source stays outside this
repository. Supplied companion acceptance replaces the unresolved external
verification prerequisite; no `pr_dev_merge` dependency or companion-edit contingency
is introduced. Any newly demonstrated substantive defect requires separate planning
and independent review.
Companion acceptance does not approve the toolkit head: complete independent source,
authority and applicable individual full-PNG review, mandatory exact-head GitHub
checks and genuine protected owner approval remain required.

The Pi remains administratively stopped and disabled. Source publication does not
authorize promoting the controller to main, deploying the worker, owner policy
approval, or activating automation. Those are separate operational decisions.
