"""Device-specific SSH signing; private material remains outside feature clones."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def ensure_key(home: Path) -> Path:
    import fcntl  # noqa: PLC0415 - the interactive launcher is Unix-only.

    directory = home / ".local/share/analytics-toolkit-agent/identity"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    key = directory / "signing_ed25519"
    with (directory / "enrollment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not key.exists():
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                check=True,
                capture_output=True,
            )
        if not key.with_suffix(".pub").is_file():
            msg = "Machine signing key is incomplete; preserve it and repair enrollment."
            raise RuntimeError(msg)
    key.chmod(0o600)
    return key


def configure(root: Path, key: Path) -> None:
    from agent_tools import sessions  # noqa: PLC0415

    state = sessions.load(root)
    state["signing_key"] = str(key.resolve())
    sessions.save(root, state)
