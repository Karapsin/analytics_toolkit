from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from agent_tools import install_sessions
from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

RUNTIMES = ("codex", "claude")
ZSH = pytest.mark.skipif(
    shutil.which("zsh") is None, reason="The interactive shell launcher requires zsh"
)


def run_wrapper(
    tmp_path: Path, runtime: str, arguments: list[str], *, inside: bool, shell: Path | None = None
) -> list[str]:
    """Run one installed wrapper against fake CLIs; shared by the parity drift test."""
    source = tmp_path / "project with spaces"
    binary = tmp_path / "bin"
    binary.mkdir(exist_ok=True)
    (source / ".venv/bin").mkdir(parents=True, exist_ok=True)
    fakes = [
        (source / ".venv/bin/python", 'printf "%s\\n" LAUNCH "$@"'),
        (binary / "git", 'printf "%s\\n" "$TEST_REPOSITORY"'),
    ]
    fakes.extend((binary / name, f'printf "%s\\n" CLI-{name} "$@"') for name in RUNTIMES)
    for path, content in fakes:
        path.write_text("#!/bin/sh\n" + content + "\n")
        path.chmod(0o755)
    if shell is None:
        shell = tmp_path / "atk_sessions.zsh"
        shell.write_text(
            (REPO_ROOT / "agent_tools/atk_sessions.zsh")
            .read_text()
            .replace('"__ATK_SOURCE__"', shlex.quote(str(source)))
        )
    return subprocess.run(
        ["zsh", "-fc", 'source "$1"; shift; "$@"', "test", str(shell), runtime, *arguments],
        text=True,
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "TEST_REPOSITORY": str(source if inside else tmp_path),
        },
    ).stdout.splitlines()


@ZSH
@pytest.mark.parametrize("runtime", RUNTIMES)
@pytest.mark.parametrize(
    ("arguments", "inside", "expected"),
    [
        ([], True, "LAUNCH"),
        (["--resume-bootstrap", "0123456789abcdef"], True, "LAUNCH"),
        (["--refresh-bootstrap-cache"], True, "LAUNCH"),
        (["exec", "task"], True, "CLI"),
        (["-p", "task"], True, "CLI"),
        (["--resume-bootstrap", "0123456789abcdef"], False, "CLI"),
    ],
)
def test_shell_routes_only_repository_bootstrap_commands(
    tmp_path, runtime, arguments, inside, expected
):
    result = run_wrapper(tmp_path, runtime, arguments, inside=inside)
    source = tmp_path / "project with spaces"
    if expected == "LAUNCH":
        assert result[0] == "LAUNCH"
        assert result[1:7] == [
            "-m",
            "agent_tools.plan_terminal",
            "--runtime",
            runtime,
            str(source),
            str(tmp_path / "bin" / runtime),
        ]
        assert result[7:] == arguments
    else:
        assert result == ["CLI-" + runtime, *arguments]


@ZSH
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_legacy_codex_shim_installs_both_wrappers_once(tmp_path, runtime):
    home = tmp_path / "home"
    home.mkdir()
    shared = home / install_sessions.SHARED
    shared.mkdir(parents=True)
    legacy = shared / "codex.zsh"
    legacy.write_text("# previous launcher\n")
    (home / ".zshrc").write_text("source " + shlex.quote(str(legacy)) + "\n")
    destination = install_sessions.install(REPO_ROOT, home)
    install_sessions.install(REPO_ROOT, home)
    assert (home / ".zshrc").read_text().count("source ") == 1
    assert legacy.read_text() == (REPO_ROOT / "agent_tools/codex.zsh").read_text()
    source = tmp_path / "project with spaces"
    destination.write_text(
        destination.read_text().replace(
            shlex.quote(str(REPO_ROOT.resolve())), shlex.quote(str(source))
        )
    )
    result = run_wrapper(tmp_path, runtime, [], inside=True, shell=legacy)
    assert result[:5] == ["LAUNCH", "-m", "agent_tools.plan_terminal", "--runtime", runtime]


def test_fresh_install_sources_the_shared_launcher(tmp_path):
    destination = install_sessions.install(REPO_ROOT, tmp_path)
    install_sessions.install(REPO_ROOT, tmp_path)
    lines = (tmp_path / ".zshrc").read_text().splitlines()
    assert lines.count("source " + shlex.quote(str(destination))) == 1
    assert "__ATK_SOURCE__" not in destination.read_text()
