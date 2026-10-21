# Release Agent Instructions

Read this file for PyPI release requests and release workflow changes.

## Version policy and reviewed preparation

Ordinary changes accumulate feature fragments or `## Unreleased` entries without
changing the package or README version. There is no entry-count threshold.
Documentation-only changes retain their no-bump behavior. Each explicit release
prepares one new four-part version, with carry at 19, and preserves historical notes.

1. Record the exact release request through the existing task-context workflow.
   An existing enrolled machine signing key is required.
2. Call `release_workflow(action="merge-dev", summary=None)` (CLI:
   `release-workflow --action merge-dev [--summary "Release summary"]`).
   The tool synchronizes origin/dev in an isolated workspace, folds fragments,
   prepares version/README/release notes, and publishes a signed preparation PR
   to dev through the existing task attachment and independent-review workflow.
   A release without unreleased entries requires a summary, including README-only
   releases. Never push a release metadata commit directly to dev.
3. Pending results include the preparation PR URL. Re-enter the same action after
   review, exact-head GitHub checks, and merge. Retries recover the same branch,
   PR and version from private `.git/atk-release.json` state.
4. Promotion requires current dev content to match the final independently reviewed
   preparation head, with successful fast-checks and agent-review on that exact SHA.
   Corrections merged through the same PR retain its prepared version and final
   reviewed notes, even when Unreleased is empty and no new summary is supplied.
   Recovery archives the previous candidate and invalidates validation receipts,
   workflow bindings, artifacts and completed stages before adopting the corrected
   head. Package and README versions must agree and Unreleased/fragments must be
   consumed. Active or uncertain old publication, published files and incompatible
   tags or refs block adoption; published conflicts require a new reviewed version.
   When newer content reaches dev, re-entering merge-dev or publish automatically
   prepares a replacement PR under the same release task. Recovery reconciles both
   indexes, saved workflows, dispatch attempts, retained artifacts and refs first.
   Active runs, uncertain dispatch within its five-minute window, unavailable
   workflows and failed evidence lookups block replacement without losing state.
   An unpublished, untagged candidate retains its version and combines prepared
   notes with new Unreleased entries and fragments. Published artifacts or local/
   remote production tags are preserved and require the next available version,
   skipping reservations on both indexes and in production tags. Every generation
   journals its source, branch, workspace and predecessor; retries recover one
   preparation PR without deleting private state. Replacement archives the old
   candidate, clears validation/publication bindings, and requires fresh review,
   exact-head checks and release validation before promotion.
   Main is fast-forwarded to the validated dev commit; it remains the release branch.
5. Run required pre-commit validation for production main, then
   `release_workflow(action="status")`. Status never creates preparation PRs or
   increments versions. It requires a reviewed candidate, runs the full Python
   compatibility/minimum-dependency/SDK matrix, and performs exhaustive
   disposable SQL integration with both ClickHouse transports. Earlier advisory
   integration results are not release receipts.
6. Call `release_workflow(action="publish")`. Without preparation it enters the
   same reviewed PR flow. Once prepared, it requires the exact production candidate's
   validation receipts, then delegates to `release_routines/pypi_release.sh`.
   Direct script invocations enforce the same candidate and receipt guard.

Low-level release-oriented `version_bump` options remain compatible metadata
preparation entrypoints, including `force_release=True` with release change types.
They validate replacements before writing and dry runs write nothing. They do
not replace the reviewed PR sequence or authorize publication.

## Publishing and safe recovery

- Preserve clean-tree checks, synchronized production main, and the production
  project name `analytics-toolkit`.
- TestPyPI uses an isolated `testpypi-<version>` branch from the prepared SHA.
  Only the project name changes to `karapsin-analytics-toolkit`; never merge that
  branch into main. A fresh clone fetches the preparation PR head explicitly and
  verifies its SHA/tree before branch creation, including after squash merge and
  deletion of the original feature branch. Reuse an existing temporary branch
  only if its parent and content match.
- GitHub trusted publishing uploads the wheel and sdist already emitted by the
  shared artifact smoke checker. Failed upload recovery reruns only failed jobs,
  preserving a successful build and its retained verified artifacts.
- Re-enter publish after pending workflows, failed jobs, or interrupted verification.
  Recovery checks the exact candidate workflow and compares published index file
  hashes with retained artifacts. Index lookup failures block uploads.
- TestPyPI dispatch attempts persist the candidate, temporary ref/SHA, attempt
  time, outcome and reconciliation deadline. Each retry first adopts an exact
  matching workflow run. Explicit rejected dispatches are retryable on the same
  candidate/version. Interrupted, timed-out and uncertain acknowledgements are
  reconciled for five minutes across invocations; after that deadline recovery
  rechecks GitHub and the index immediately before one replacement dispatch.
  Legacy request markers start this window on first recovery. API/index errors
  block retry. Concurrent release actions are excluded by the private state lock.
  A delayed original dispatch may still create a duplicate; this bounded retry
  policy does not guarantee exactly-once scheduling.
- Never move an existing production tag or overwrite published artifacts. A tag
  mismatch or changed published file requires a new candidate/version. An existing
  GitHub release is resumed through its workflow rather than recreated.
- Verify each index artifact in a fresh temporary environment outside the checkout;
  imports must resolve from site-packages. The opposite index's publish job must
  remain skipped. Production publishing requires verified TestPyPI artifacts for
  the same candidate.
- Delete temporary TestPyPI branches only after successful production verification.
  Completed stages are bound to the candidate SHA and version in the private state.

Private controller implementation workers do not execute validation. GitHub runs
fast checks, independent review, compatibility, quality, coverage and artifact
checks after publication. Controller edits retain owner exact-head policy approval.
An actual release additionally requires exhaustive validation before TestPyPI.
