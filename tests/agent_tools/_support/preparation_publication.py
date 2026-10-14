from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_tools import sessions, task_context
from agent_tools import release_preparation as preparation
from release_routines.lib import release_candidate as candidate
from tests.agent_tools._support.mcp import _write_minimal_repo_files


def _task_directory(root):
    path = root / "tasks"
    path.mkdir(exist_ok=True)
    return path


def _publication_state(monkeypatch, tmp_path):
    root = _write_minimal_repo_files(tmp_path / "project")
    work = tmp_path / "workspace"
    work.mkdir()
    (work / ".git").mkdir()
    monkeypatch.setattr(preparation, "state_path", lambda root: root / preparation.STATE)
    monkeypatch.setattr(sessions, "state_path", lambda root: root / ".git" / sessions.STATE)

    monkeypatch.setattr(task_context, "directory", _task_directory)
    task_context.record(work, "Prepare a PyPI release.")
    context = task_context.read(work)
    proto = task_context.protocol()
    version, files, fragments = preparation._metadata_files(root, "Release notes")
    state = {
        "task_id": context["task_id"],
        "task_digest": proto.digest(proto.public_context(context)),
        "workspace": str(work),
        "branch": "codex/release",
        "source_sha": "source",
        "candidate_sha": "author",
        "candidate_tree": "author-tree",
        "version": version,
        "files": files,
        "fragments": fragments,
        "pr": None,
        "stages": {"validation": "author"},
    }
    preparation.save(root, state)
    sessions.save(
        work,
        {"phase": "feature", "branch": state["branch"], "publication_sha": "author"},
    )
    pr = {
        "number": 12,
        "url": "https://example/pr/12",
        "state": "OPEN",
        "mergedAt": None,
        "headRefName": state["branch"],
        "baseRefName": "dev",
        "headRefOid": "corrected",
        "body": proto.render(context),
        "statusCheckRollup": [
            {"name": name, "conclusion": "SUCCESS"} for name in ("fast-checks", "agent-review")
        ],
    }

    return SimpleNamespace(
        root=root,
        work=work,
        state=state,
        pr=pr,
        calls=[],
        signatures=[],
        faults={"discovery": None, "ancestry": False, "sync": False},
        matches=[{"number": 12}],
        version=version,
        files=files,
        context=context,
    )


def _list_prs(item, args):
    assert "--state" in args
    assert args[args.index("--state") + 1] == "all"
    if item.faults["discovery"]:
        raise RuntimeError(item.faults["discovery"])
    return json.dumps(item.matches)


def _view_pr(item, args):
    assert args[3] == "12"
    return json.dumps(item.pr)


def _branch(args):
    if args != ("git", "branch", "--show-current"):
        pytest.fail("Unexpected write or command: " + repr(args))
    return "codex/release"


def _log(context, args):
    assert args[-1] == "author"
    return "Prepare release" + task_context.protocol().trailers(context)


def _ancestry(item, args):
    if args[:3] != ("git", "merge-base", "--is-ancestor"):
        pytest.fail("Unexpected write or command: " + repr(args))
    assert args[3:] == ("author", "corrected")
    if item.faults["ancestry"]:
        message = "unrelated ancestry"
        raise RuntimeError(message)
    return ""


def _command_runner(item, files, context):
    commands = {
        ("gh", "pr", "list"): lambda args: _list_prs(item, args),
        ("gh", "pr", "view"): lambda args: _view_pr(item, args),
        ("git", "fetch"): lambda args: "",
        ("git", "ls-remote"): lambda args: "",
        ("git", "tag"): lambda args: "",
        ("git", "branch"): _branch,
        ("git", "log"): lambda args: _log(context, args),
        ("git", "merge-base"): lambda args: _ancestry(item, args),
        ("git", "show"): lambda args: files[args[2].split(":", 1)[1]].strip(),
        ("git", "ls-tree"): lambda args: "",
    }

    def run(root, *args):
        item.calls.append(args)
        handler = commands.get(args[:3] if args[:1] == ("gh",) else args[:2])
        if handler is None:
            pytest.fail("Unexpected write or command: " + repr(args))
        return handler(args)

    return run


def _synchronize(item, root):
    if item.faults["sync"]:
        message = "refresh interrupted"
        raise RuntimeError(message)
    receipt = sessions.load(root)
    receipt.update(phase="planning", branch="dev")
    receipt.pop("pr", None)
    receipt.pop("publication_sha", None)
    sessions.save(root, receipt)
    return {"status": "synchronized"}


def make_publication(monkeypatch, tmp_path):
    item = _publication_state(monkeypatch, tmp_path)
    monkeypatch.setattr(sessions, "run", _command_runner(item, item.files, item.context))
    monkeypatch.setattr(sessions, "clean", lambda root: None)
    monkeypatch.setattr(sessions, "synchronize", lambda root: _synchronize(item, root))
    monkeypatch.setattr(sessions, "verify_signature", lambda root, sha: item.signatures.append(sha))
    monkeypatch.setattr(sessions, "push", lambda root: pytest.fail("republished existing PR"))
    monkeypatch.setattr(preparation, "_tree", lambda root, sha: "corrected-tree")
    monkeypatch.setattr(candidate, "index_files", lambda *args: {})
    monkeypatch.setattr(candidate, "_runs", lambda *args: [])
    return item
