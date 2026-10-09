"""Install the narrowly scoped shell function; preserve the existing shell configuration."""

from __future__ import annotations

import shlex
from pathlib import Path


def install(root: Path, home: Path) -> Path:
    destination = home / ".local/share/analytics-toolkit-agent/codex.zsh"
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = (root / "agent_tools/codex.zsh").read_text()
    destination.write_text(content.replace('"__ATK_SOURCE__"', shlex.quote(str(root.resolve()))))
    rc = home / ".zshrc"
    original = rc.read_text() if rc.exists() else ""
    line = "source " + shlex.quote(str(destination))
    if line not in original.splitlines():
        # Following a symlink is intentional: do not replace a user's managed dotfile.
        with rc.open("a") as output:
            output.write("\n# Isolated analytics-toolkit Codex sessions\n" + line + "\n")
    return destination


if __name__ == "__main__":
    print(install(Path(__file__).resolve().parents[1], Path.home()))
