"""Small local environments and process fakes for bootstrap behavior tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_tools import bootstrap_env, mcp_server


def project(root: Path) -> Path:
    (root / "agent_tools").mkdir(parents=True)
    (root / "agent_tools/requirements-mcp.txt").write_text("fixture-runtime==1\n")
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n'
        '[project]\nname = "fixture-project"\nversion = "1.0"\ndependencies = []\n'
    )
    (root / "tox.ini").write_text("[tox]\n")
    return root


def prepare(root: Path, runner: Any, *, full: bool = True) -> tuple[list, dict]:
    return bootstrap_env.prepare(
        root,
        project=full,
        commands=mcp_server._environment_commands(root, full),
        run=runner,
    )


def result(command: dict, *, ok: bool = True) -> dict:
    return {
        "ok": ok,
        "command": command["display"],
        "returncode": 0 if ok else 1,
        "stdout": "",
        "stderr": "" if ok else "failed",
        "summary": "completed",
    }


class Runner:
    def __init__(self) -> None:
        self.commands: list[dict] = []
        self.fail: str | None = None
        self.interrupt = False

    def __call__(self, _root: Path, command: dict) -> dict:
        self.commands.append(command)
        if command.get("network"):
            if self.interrupt:
                raise KeyboardInterrupt
            wheels = Path(command["args"][command["args"].index("--wheel-dir") + 1])
            (wheels / "fixture_runtime-1-py3-none-any.whl").write_bytes(b"complete-wheel")
        return result(command, ok=command["display"] != self.fail)

    @property
    def downloads(self) -> list[dict]:
        return [command for command in self.commands if command.get("network")]


def manifest(cache: Path) -> tuple[Path, dict]:
    path = next(cache.glob("*/ready.json"))
    return path, json.loads(path.read_text())
