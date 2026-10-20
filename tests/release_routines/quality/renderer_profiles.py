from __future__ import annotations

import configparser
import re
import shlex

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from tests._support.paths import REPO_ROOT


def _configuration():
    configuration = configparser.RawConfigParser()
    configuration.read(REPO_ROOT / "tox.ini")
    return configuration


def _workflow_installs():
    workflow = (REPO_ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    matrix = workflow.split("\n  tests:\n", 1)[1].split("\n  sdk-conformance:\n", 1)[0]
    commands = re.findall(r"^        run: (python -m pip install .+)$", matrix, re.MULTILINE)
    arguments = [shlex.split(command) for command in commands]
    installs = [command for command in arguments if "-e" in command]
    assert len(installs) == 2
    return {"latest": installs[0], "min": installs[1]}


def _tox_dependencies(configuration, mode):
    dependencies = []
    for line in configuration["testenv"]["deps"].splitlines():
        factor, separator, requirement = line.strip().partition(": ")
        if not separator:
            dependencies.append(factor)
        elif factor == mode:
            dependencies.append(requirement)
    return dependencies


def _renderers(arguments):
    return [Requirement(argument) for argument in arguments if argument.startswith("resvg-py")]


def _assert_renderer_selection(requirements, python):
    environment = {"python_version": python, "python_full_version": f"{python}.0"}
    active = [
        requirement
        for requirement in requirements
        if requirement.marker is None or requirement.marker.evaluate(environment)
    ]
    assert len(active) == 1, "Expected one active renderer requirement"
    assert all(requirement.marker is not None for requirement in requirements)
    expected = "==0.3.2" if python == "3.9" else ">=0.5,<0.6"
    assert active[0].specifier == SpecifierSet(expected)


@pytest.mark.parametrize(
    ("python", "mode"),
    [
        ("3.9", "latest"),
        ("3.9", "min"),
        ("3.10", "latest"),
        ("3.11", "latest"),
        ("3.12", "latest"),
        ("3.13", "latest"),
        ("3.14", "latest"),
    ],
)
def test_workflow_and_tox_select_one_compatible_renderer(python, mode):
    configuration = _configuration()
    workflow_arguments = _workflow_installs()[mode]
    tox_dependencies = _tox_dependencies(configuration, mode)
    for arguments in (workflow_arguments, tox_dependencies):
        requirements = _renderers(arguments)
        _assert_renderer_selection(requirements, python)
        assert "Pillow>=10,<13" in arguments
        conflicting = [*requirements, Requirement("resvg-py>=0.5,<0.6")]
        with pytest.raises(AssertionError, match="Expected one active renderer requirement"):
            _assert_renderer_selection(conflicting, python)
    if mode == "min":
        constraint = "constraints/py39-min.txt"
        assert workflow_arguments[workflow_arguments.index("-c") + 1] == constraint
        assert f"-c {constraint}" in tox_dependencies


def test_renderer_correction_preserves_coverage_sdk_and_required_commands():
    configuration = _configuration()
    coverage = configuration["testenv:coverage"]
    assert _renderers(coverage["deps"].splitlines()) == [Requirement("resvg-py>=0.5,<0.6")]
    assert coverage["base_python"] == "{env:PYTHON311:python3.11}"
    assert "coverage run -m pytest -q" in coverage["commands"].splitlines()
    assert "coverage json -o coverage.json" in coverage["commands"].splitlines()
    for section in (configuration["testenv"], coverage):
        commands = section["commands"].splitlines()
        assert "python -m pytest -q tests/agent_tools/underline_rasterization.py" in commands
    assert "pytest -q" in configuration["testenv"]["commands"].splitlines()
    for suffix, version in (("31", "3.1.0"), ("32", "3.2.0")):
        sdk = configuration[f"testenv:py310-sdk{suffix}"]
        assert f"datalens-sdk=={version}" in sdk["deps"].splitlines()
        assert _renderers(sdk["deps"].splitlines()) == []
        assert sdk["commands"] == "pytest -q tests/datalens_utils"
    mcp_requirements = (REPO_ROOT / "agent_tools/requirements-mcp.txt").read_text(encoding="utf-8")
    assert _renderers(mcp_requirements.splitlines()) == [Requirement("resvg-py>=0.5,<0.6")]
