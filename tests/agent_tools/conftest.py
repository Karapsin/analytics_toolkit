"""Keep agent tool tests collectable everywhere and independent of the host CLI."""

from __future__ import annotations

import sys

import pytest

# resvg-py>=0.5 publishes no Python 3.9 builds, so the actual-renderer regressions and
# the diagnostics that import them run only where the pinned renderer can install.
collect_ignore = []
if sys.version_info < (3, 10):
    collect_ignore = ["underline_rasterization.py", "raster_observations.py"]

# Reviewer selection must not depend on the CLI that happens to run pytest.
HOST_RUNTIME_ENV = (
    "ATK_LOCAL_REVIEWER",
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CODEX_THREAD_ID",
)


@pytest.fixture(autouse=True)
def isolated_host_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in HOST_RUNTIME_ENV:
        monkeypatch.delenv(name, raising=False)
