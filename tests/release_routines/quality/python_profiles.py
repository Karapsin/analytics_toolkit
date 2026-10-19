from __future__ import annotations

import configparser
import os
import re
import subprocess

from tests._support.paths import REPO_ROOT


def test_managed_matrix_runs_without_python38_and_preserves_other_profiles(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    interpreter = tmp_path / "interpreter" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o755)
    versions = tmp_path / "versions"
    arguments = tmp_path / "arguments"
    scripts = {
        "pyenv": (
            '#!/bin/sh\nprintf "%s\\n" "$2" >> "$MATRIX_INTERPRETER_LOG"\n'
            'case "$2" in 3.8*) exit 91;; esac\n'
            'printf "%s\\n" "$MATRIX_PYTHON_PREFIX"\n'
        ),
        "tox": '#!/bin/sh\nprintf "%s\\n" "$@" > "$MATRIX_TOX_ARGS"\n',
    }
    for name, text in scripts.items():
        executable = binaries / name
        executable.write_text(text)
        executable.chmod(0o755)
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "release_routines/pre_commit_checks.sh"), "--matrix"],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{binaries}:/usr/bin:/bin",
            "MATRIX_INTERPRETER_LOG": str(versions),
            "MATRIX_PYTHON_PREFIX": str(interpreter.parent.parent),
            "MATRIX_TOX_ARGS": str(arguments),
            "PRECOMMIT_PARALLELISM": "2",
        },
    )
    assert result.returncode == 0, result.stderr
    assert {".".join(version.split(".")[:2]) for version in versions.read_text().splitlines()} == {
        "3.9",
        "3.10",
        "3.11",
        "3.12",
        "3.13",
        "3.14",
    }
    actual = arguments.read_text().splitlines()
    assert actual[:4] == ["run-parallel", "-p", "2", "-e"]
    assert set(actual[4].split(",")) == {
        "py39-latest",
        "py39-min",
        "py310-latest",
        "py310-sdk31",
        "py310-sdk32",
        "py312-latest",
        "py313-latest",
        "py314-latest",
    }


def test_github_and_tox_profiles_exclude_python38_and_retain_minimum_dependencies():
    configuration = configparser.RawConfigParser()
    configuration.read(REPO_ROOT / "tox.ini")
    environments = configuration["tox"]["env_list"].split()
    assert not any(name.startswith("py38") for name in environments)
    assert "py39-min" in environments
    assert "PYTHON38" not in configuration["testenv"]["base_python"]
    assert "constraints/py39-min.txt" in configuration["testenv"]["commands_pre"]
    assert "min: python -m pip check" in configuration["testenv"]["commands"]
    workflow = (REPO_ROOT / ".github/workflows/tests.yml").read_text()
    matrix = workflow.split("\n  tests:\n", 1)[1].split("\n  sdk-conformance:\n", 1)[0]
    profiles = re.findall(r'python-version: "([0-9.]+)"\n\s+env_name: ([a-z0-9-]+)', matrix)
    assert set(profiles) == {
        ("3.9", "py39-latest"),
        ("3.10", "py310-latest"),
        ("3.11", "py311-latest"),
        ("3.12", "py312-latest"),
        ("3.13", "py313-latest"),
        ("3.14", "py314-latest"),
        ("3.9", "py39-min"),
    }
    assert "constraints/py39-min.txt" in matrix
    assert "python -m pip check" in matrix
