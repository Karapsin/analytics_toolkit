# Approved workflow scope

This records the plan approved by the user's final “Implement the plan.” request.
Earlier PR #9 recovery and Pi automation work were cancelled; they are historical
task context, not implementation requirements for this change.

The user's stated changes were:

- Remove Pi feature requests and feature development through Telegram.
- Keep initial review/correction on the machine developing the feature, outside GitHub.
- Keep the orchestrator. Restrict the GitHub coding role to conflict resolution.
- Never return published tasks to the developing machine.
- Use the newest supported Python for dev checks to reduce turnaround time.
- Use separate Pi repair agents for post-merge failures, as with integration failures.

The approved implementation plan additionally assigns pre-merge CI failures to
the separate Pi repair role and keeps full compatibility validation after merges
and during releases. Python 3.14 is the package's newest supported runtime;
lint/type tooling may retain its pinned interpreter. Coverage, artifacts, fast
regressions, independent review and SQL Explorer visual requirements remain.

Local publication requires a fresh read-only reviewer, separate from the author,
to approve the exact head, dev base and user-task digest. Rejected candidates stay
local for correction and another fresh review. The signed review receipt travels
with the PR and is verified against host-owned enrollment. Changed heads/bases
after publication receive fresh Pi review and any repair remains Pi-owned.

The orchestrator coordinates checks, provenance, one writer per PR and merging.
Separate repair agents retain prior evidence, deduplicate incidents, and pause
after three unsuccessful repair candidates. Infrastructure/authentication failures
remain operational retries or holds. Partial repair cannot reset the incident's
allowance. Authenticated owner controls resume paused incidents.

Implementation spans this toolkit repository and the companion `Karapsin/self_host`
worker. Private, task-bound review evidence supplies the companion commit, complete
change patch, independent source-review result and exact-SHA CI result. That
evidence is available to the local reviewer without copying private companion
source into this repository.

The Pi remains administratively stopped and disabled. Source publication does not
authorize promoting the controller to main, deploying the worker, owner policy
approval, or activating automation. Those are separate operational decisions.
