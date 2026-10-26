"""Install the narrowly scoped shell functions; preserve the existing shell configuration."""

from __future__ import annotations

import shlex
from pathlib import Path

SHARED = ".local/share/analytics-toolkit-agent"


def install(root: Path, home: Path) -> Path:
    directory = home / SHARED
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "atk_sessions.zsh"
    content = (root / "agent_tools/atk_sessions.zsh").read_text()
    destination.write_text(content.replace('"__ATK_SOURCE__"', shlex.quote(str(root.resolve()))))
    # Earlier installs sourced codex.zsh; keep that line working as a shim so a
    # shell configuration never sources the launchers twice.
    legacy = directory / "codex.zsh"
    legacy.write_text((root / "agent_tools/codex.zsh").read_text())
    rc = home / ".zshrc"
    original = rc.read_text() if rc.exists() else ""
    lines = original.splitlines()
    line = "source " + shlex.quote(str(destination))
    if line not in lines and "source " + shlex.quote(str(legacy)) not in lines:
        # Following a symlink is intentional: do not replace a user's managed dotfile.
        with rc.open("a") as output:
            output.write("\n# Isolated analytics-toolkit Codex and Claude Code sessions\n")
            output.write(line + "\n")
    return destination


if __name__ == "__main__":
    print(install(Path(__file__).resolve().parents[1], Path.home()))
