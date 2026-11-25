"""Author review plans use the workflow entrypoint without launching a review."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_tools import local_review, mcp_server
from tests.agent_tools._support.mcp import _write_minimal_repo_files


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative-to-cwd"])
@pytest.mark.parametrize("notice_key", ["summary", "message", None])
def test_git_workflow_review_plan_passes_path_and_planner_notice_without_publication(
    monkeypatch, tmp_path, relative, notice_key
):
    root = _write_minimal_repo_files(tmp_path / "project")
    (root / "uncommitted-procedure.txt").write_text("dirty planning worktree\n")
    manifest = tmp_path / "author-plan.json"
    manifest.write_text('{"chunks": []}')
    monkeypatch.chdir(tmp_path)
    selected = Path(manifest.name) if relative else manifest
    expected = {"status": "review_plan_installed", "head": "f" * 40}
    if notice_key is not None:
        expected[notice_key] = "Author-assigned chunks installed; no source approval."
    seen = []

    def install(root_path, plan_path):
        seen.append((root_path, plan_path))
        assert plan_path.read_text() == manifest.read_text()
        return expected

    def forbidden(*_args, **_kwargs):
        pytest.fail("Installing a review plan must not review, check, commit, or publish")

    monkeypatch.setattr(local_review, "plan_review", install)
    monkeypatch.setattr(local_review, "review", forbidden)
    monkeypatch.setattr(local_review, "discover", forbidden)
    monkeypatch.setattr(mcp_server, "_feature_commit_context", forbidden)
    monkeypatch.setattr(mcp_server, "_verify_precommit_success", forbidden)
    monkeypatch.setattr(mcp_server, "_push_dev_workflow", forbidden)

    result = mcp_server.git_workflow("review-plan", review_plan_path=str(selected), root=str(root))

    assert result["ok"] is True
    assert result["result"] == expected
    assert result["summary"] == (
        expected[notice_key] if notice_key is not None else "Review plan installed."
    )
    assert result["input"]["review_plan_path"] == str(selected)
    assert seen == [(root, selected)]
    assert (root / "uncommitted-procedure.txt").read_text() == "dirty planning worktree\n"


@pytest.mark.parametrize("path", [None, "", " \t"])
def test_git_workflow_review_plan_requires_an_explicit_nonblank_path(monkeypatch, tmp_path, path):
    root = _write_minimal_repo_files(tmp_path / "project")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Missing review-plan input must not reach the planner")

    monkeypatch.setattr(mcp_server, "_session_workflow_result", forbidden)

    result = mcp_server.git_workflow("review-plan", review_plan_path=path, root=str(root))

    assert result["ok"] is False
    assert result["blockers"] == [
        {"phase": "validate", "message": "review_plan_path is required for review-plan"}
    ]


@pytest.mark.parametrize("problem", ["missing", "directory", "encoding", "json"])
def test_git_workflow_review_plan_reports_file_validation_failures(monkeypatch, tmp_path, problem):
    root = _write_minimal_repo_files(tmp_path / "project")
    manifest = tmp_path / "author-plan.json"
    if problem == "directory":
        manifest.mkdir()
    elif problem == "encoding":
        manifest.write_bytes(b"\xff")
    elif problem == "json":
        manifest.write_text("not-json")

    def install(_root, path):
        return json.loads(path.read_text(encoding="utf-8"))

    monkeypatch.setattr(local_review, "plan_review", install)

    result = mcp_server.git_workflow("review-plan", review_plan_path=str(manifest), root=str(root))

    assert result["ok"] is False
    assert result["summary"] == "Session workflow blocked."
    assert result["blockers"][0]["phase"] == "review-plan"
    assert result["blockers"][0]["message"]


def test_git_workflow_review_plan_cli_forwards_author_manifest(monkeypatch, tmp_path):
    captured = {}

    def workflow(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "result": {"status": "review_plan_installed"}}

    monkeypatch.setattr(mcp_server, "git_workflow", workflow)
    parser = mcp_server._build_cli_parser()
    args = parser.parse_args(
        [
            "git-workflow",
            "review-plan",
            "--review-plan",
            "author-plan.json",
            "--root",
            str(tmp_path),
        ]
    )

    result = args.handler(args)

    assert result == {"ok": True, "result": {"status": "review_plan_installed"}}
    assert captured["action"] == "review-plan"
    assert captured["review_plan_path"] == "author-plan.json"
    assert captured["root"] == str(tmp_path)


def test_git_workflow_review_plan_cli_missing_manifest_returns_structured_failure(tmp_path, capsys):
    root = _write_minimal_repo_files(tmp_path / "project")

    result = mcp_server._handle_cli_call(["git-workflow", "review-plan", "--root", str(root)])

    assert result == 1
    response = json.loads(capsys.readouterr().out)
    assert response["ok"] is False
    assert response["blockers"] == [
        {"phase": "validate", "message": "review_plan_path is required for review-plan"}
    ]


def test_session_workflow_review_plan_without_a_path_cannot_fall_through_to_sync(tmp_path):
    with pytest.raises(ValueError, match="review_plan_path is required for review-plan"):
        mcp_server._session_workflow_result(tmp_path, "review-plan", None, None)
