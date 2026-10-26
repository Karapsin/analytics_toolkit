"""Agent tooling tests are independent of the CLI that happens to run pytest."""

from __future__ import annotations

import pytest

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
