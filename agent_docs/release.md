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
4. Promotion requires current dev content to match the reviewed candidate.
   Intervening changes require a new preparation PR and invalidate validation.
   Main is fast-forwarded to the validated dev commit; it remains the release branch.
5. Run required pre-commit validation for production main, then
   `release_workflow(action="status")`. Status never creates preparation PRs or
   increments versions. It requires a reviewed candidate and performs exhaustive
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
  branch into main. Reuse an existing branch only if its parent and content match.
- GitHub trusted publishing uploads the wheel and sdist already emitted by the
  shared artifact smoke checker. Failed upload recovery reruns only failed jobs,
  preserving a successful build and its retained verified artifacts.
- Re-enter publish after pending workflows, failed jobs, or interrupted verification.
  Recovery checks the exact candidate workflow and compares published index file
  hashes with retained artifacts. Index lookup failures block uploads.
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
