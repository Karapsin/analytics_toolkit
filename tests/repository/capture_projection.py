from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests._support.paths import REPO_ROOT
from tests.repository._support.capture_native import git
from tests.repository._support.capture_profile import T, load


def test_actual_projection_promotes_only_literal_profile_workflow(tmp_path):
    workflow = tmp_path / T.WORKFLOW
    workflow.parent.mkdir(parents=True)
    package = tmp_path / "analytics_toolkit/version.py"
    package.parent.mkdir()
    package.write_text("released\n")
    git(tmp_path, "init", "--initial-branch=fixture")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.test")
    git(tmp_path, "add", "--all")
    git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-m", "Synthetic released base")
    base = git(tmp_path, "rev-parse", "HEAD")
    raw = (REPO_ROOT / T.WORKFLOW).read_bytes()
    workflow.write_bytes(raw)
    package.write_text("unreleased\n")
    git(tmp_path, "add", "--all")
    git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-m", "Synthetic feature")
    head = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "--detach", base)
    controller, promotion = load("controller"), load("automation_promotion")
    adapter = SimpleNamespace(policy_path=controller.policy_path)
    adapter.command = lambda *args, cwd, **kwargs: git(
        cwd, *args[1:], raw_stdout=kwargs.get("raw_stdout", False)
    )
    assert promotion.project(adapter, tmp_path, head, base, [T.WORKFLOW])
    tree = git(tmp_path, "write-tree")
    assert git(tmp_path, "show", tree + ":" + T.WORKFLOW).encode() == raw.rstrip(b"\n")
    assert git(tmp_path, "show", tree + ":analytics_toolkit/version.py") == "released"
    with pytest.raises(ValueError, match="Package paths"):
        promotion.project(adapter, tmp_path, head, base, [T.WORKFLOW + ".bak"])
