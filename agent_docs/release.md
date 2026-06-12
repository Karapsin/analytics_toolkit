# Release Agent Instructions

Read this file when the user asks to update, publish, or release the package on
PyPI, or when changing release workflow behavior.

## PyPI Release Rules

When the user asks to update, publish, or release the package on PyPI, run the
complete publishing workflow unless they explicitly ask for a narrower action:

- Use `release_routines/pypi_release.sh` for the full publishing workflow. It runs TestPyPI publishing and artifact verification first, then real PyPI publishing and artifact verification. Do not call the internal scripts under `release_routines/scripts/` unless the user explicitly asks for a narrower release action or the top-level script itself is blocked.
- If the release only changes documentation or PyPI README content, bump the package version for the release artifact and update `docs/CHANGELOG.md` even though ordinary docs-only changes must not bump versions. PyPI artifacts are immutable, so publishing changed package metadata requires a new version.
- Publish the candidate version to TestPyPI first through GitHub Actions trusted publishing.
- Verify the TestPyPI artifact in a fresh temporary virtual environment from outside the repository checkout, and confirm imports resolve from that environment's `site-packages`.
- Publish the same production package/version to real PyPI through the GitHub release workflow.
- Verify the real PyPI artifact in a fresh temporary virtual environment from outside the repository checkout, and confirm imports resolve from that environment's `site-packages`.
- Check the GitHub Actions jobs after each publish. TestPyPI publishes must leave the real PyPI job skipped; real PyPI publishes must leave the TestPyPI job skipped.
- TestPyPI trusted publishing is currently configured for the temporary project name `karapsin-analytics-toolkit`, while the production PyPI project name is `analytics-toolkit`. For TestPyPI, create a temporary `testpypi-<version>` branch from the exact release candidate commit and change only `[project].name` in `pyproject.toml` to `karapsin-analytics-toolkit`.
- Keep TestPyPI package-name changes on temporary branches only. Do not merge temporary TestPyPI branches into `main`, and publish production PyPI only from the unchanged production project metadata.
- When verifying TestPyPI artifacts, install `karapsin-analytics-toolkit==<version>` but still confirm the import package is `analytics_toolkit` from `site-packages`.
- After every successful deployment to real PyPI and artifact verification, delete all temporary TestPyPI branches locally and remotely, including old `testpypi-*` branches.
