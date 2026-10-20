from __future__ import annotations

import os
import shlex
import shutil
import subprocess

import pytest

from tests._support.paths import REPO_ROOT


@pytest.mark.skipif(
    shutil.which("zsh") is None, reason="The interactive shell launcher requires zsh"
)
@pytest.mark.parametrize(
    ("arguments", "inside", "expected"),
    [
        ([], True, "LAUNCH"),
        (["--resume-bootstrap", "0123456789abcdef"], True, "LAUNCH"),
        (["--refresh-bootstrap-cache"], True, "LAUNCH"),
        (["exec", "task"], True, "CLI"),
        (["--resume-bootstrap", "0123456789abcdef"], False, "CLI"),
    ],
)
def test_shell_routes_only_repository_bootstrap_commands(tmp_path, arguments, inside, expected):
    source = tmp_path / "project with spaces"
    binary = tmp_path / "bin"
    binary.mkdir()
    (source / ".venv/bin").mkdir(parents=True)
    for path, content in [
        (source / ".venv/bin/python", 'printf "%s\\n" LAUNCH "$@"'),
        (binary / "codex", 'printf "%s\\n" CLI "$@"'),
        (binary / "git", 'printf "%s\\n" "$TEST_REPOSITORY"'),
    ]:
        path.write_text("#!/bin/sh\n" + content + "\n")
        path.chmod(0o755)
    shell = tmp_path / "launcher.zsh"
    shell.write_text(
        (REPO_ROOT / "agent_tools/codex.zsh")
        .read_text()
        .replace('"__ATK_SOURCE__"', shlex.quote(str(source)))
    )
    result = subprocess.run(
        ["zsh", "-fc", 'source "$1"; shift; codex "$@"', "test", str(shell), *arguments],
        text=True,
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "TEST_REPOSITORY": str(source if inside else tmp_path),
        },
    ).stdout.splitlines()
    assert result[0] == expected
    if expected == "LAUNCH":
        assert result[1:5] == [
            "-m",
            "agent_tools.plan_terminal",
            str(source),
            str(binary / "codex"),
        ]
        assert result[5:] == arguments
    else:
        assert result[1:] == arguments
