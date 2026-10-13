from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from release_routines.lib import release_candidate as candidate

from agent_tools import release_preparation as preparation
from agent_tools import sessions, task_context
from tests.agent_tools._support.mcp import Path, _write_minimal_repo_files, mcp_server


@pytest.fixture
def publication(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    root = _write_minimal_repo_files(tmp_path / "project")
    work = tmp_path / "workspace"
    work.mkdir()
    (work / ".git").mkdir()
    monkeypatch.setattr(preparation, "state_path", lambda root: root / preparation.STATE)
    monkeypatch.setattr(sessions, "state_path", lambda root: root / ".git" / sessions.STATE)

    def directory(root):
        path = root / "tasks"
        path.mkdir(exist_ok=True)
        return path

    monkeypatch.setattr(task_context, "directory", directory)
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
            {"name": name, "conclusion": "SUCCESS"}
            for name in ("fast-checks", "agent-review")
        ],
    }
    calls = []
    signatures = []
    faults = {"discovery": None, "ancestry": False, "sync": False}
    matches = [{"number": 12}]

    def run(root, *args):
        calls.append(args)
        if args[:3] == ("gh", "pr", "list"):
            assert "--state" in args and args[args.index("--state") + 1] == "all"
            if faults["discovery"]:
                raise RuntimeError(faults["discovery"])
            return json.dumps(matches)
        if args[:3] == ("gh", "pr", "view"):
            assert args[3] == "12"
            return json.dumps(pr)
        if args[:2] in {("git", "fetch"), ("git", "ls-remote"), ("git", "tag")}:
            return ""
        if args == ("git", "branch", "--show-current"):
            return "codex/release"
        if args[:2] == ("git", "log"):
            assert args[-1] == "author"
            return "Prepare release" + proto.trailers(context)
        if args[:3] == ("git", "merge-base", "--is-ancestor"):
            assert args[3:] == ("author", "corrected")
            if faults["ancestry"]:
                message = "unrelated ancestry"
                raise RuntimeError(message)
            return ""
        if args[:2] == ("git", "show"):
            return files[args[2].split(":", 1)[1]].strip()
        if args[:2] == ("git", "ls-tree"):
            return ""
        pytest.fail("Unexpected write or command: " + repr(args))

    def synchronize(root):
        if faults["sync"]:
            message = "refresh interrupted"
            raise RuntimeError(message)
        receipt = sessions.load(root)
        receipt.update(phase="planning", branch="dev")
        receipt.pop("pr", None)
        receipt.pop("publication_sha", None)
        sessions.save(root, receipt)
        return {"status": "synchronized"}

    monkeypatch.setattr(sessions, "run", run)
    monkeypatch.setattr(sessions, "clean", lambda root: None)
    monkeypatch.setattr(sessions, "synchronize", synchronize)
    monkeypatch.setattr(sessions, "verify_signature", lambda root, sha: signatures.append(sha))
    monkeypatch.setattr(sessions, "push", lambda root: pytest.fail("republished existing PR"))
    monkeypatch.setattr(preparation, "_tree", lambda root, sha: "corrected-tree")
    monkeypatch.setattr(candidate, "index_files", lambda *args: {})
    monkeypatch.setattr(candidate, "_runs", lambda *args: [])
    return SimpleNamespace(
        root=root,
        work=work,
        state=state,
        pr=pr,
        calls=calls,
        signatures=signatures,
        faults=faults,
        matches=matches,
        version=version,
    )


@pytest.mark.parametrize("merged", [False, True])
@pytest.mark.parametrize("identifier", [False, True])
def test_created_pr_recovers_correction_without_republishing(publication, merged, identifier):
    item = publication
    receipt = sessions.load(item.work)
    if identifier:
        receipt["pr"] = 12
    else:
        receipt.pop("publication_sha")
    sessions.save(item.work, receipt)
    if merged:
        item.pr.update(state="MERGED", mergedAt="today")
    validation = item.root / mcp_server.CHECK_STATE_FILE
    validation.parent.mkdir(exist_ok=True)
    validation.write_text("stale author validation")

    result = preparation.prepare(item.root)
    saved = preparation.load(item.root)
    assert result["status"] == ("prepared" if merged else "pending")
    assert result["pr"] == saved["pr"] == 12
    assert result["version"] == item.version
    assert saved["candidate_sha"] == ("corrected" if merged else "author")
    assert item.signatures == ["author"]
    session = sessions.load(item.work)
    assert session["phase"] == "planning"
    assert session["handoffs"][-1]["owner"] == "pi"
    assert session["handoffs"][-1]["sha"] == "author"
    assert ("git", "merge-base", "--is-ancestor", "author", "corrected") in item.calls
    if merged:
        assert not validation.exists()
        assert saved["stages"] == {"reviewed": "corrected"}
        assert (item.root / "atk-release.json.author").exists()
    assert preparation.prepare(item.root) == result
    assert item.signatures == ["author"]


@pytest.mark.parametrize("moment", ["before-ownership", "handed-off", "planning"])
def test_handoff_interruption_retains_pr_version_and_durable_ownership(
    publication,
    monkeypatch: pytest.MonkeyPatch,
    moment: str,
):
    item = publication
    real_save = sessions.save
    real_handoff = sessions.handoff
    interrupted = []

    def save(root, state):
        real_save(root, state)
        if moment == "before-ownership" and state.get("pr") == 12 and not interrupted:
            interrupted.append(True)
            message = "interrupted after discovery"
            raise RuntimeError(message)

    def handoff(root):
        result = real_handoff(root)
        if moment != "before-ownership" and not interrupted:
            interrupted.append(True)
            message = "interrupted after handoff"
            raise RuntimeError(message)
        return result

    item.faults["sync"] = moment == "handed-off"
    monkeypatch.setattr(sessions, "save", save)
    monkeypatch.setattr(sessions, "handoff", handoff)
    with pytest.raises(RuntimeError, match="interrupted"):
        preparation.prepare(item.root)
    assert preparation.load(item.root)["pr"] is None
    receipt = sessions.load(item.work)
    expected_phase = "feature" if moment == "before-ownership" else moment.replace("-", "_")
    assert receipt["phase"] == expected_phase
    item.faults["sync"] = False
    result = preparation.prepare(item.root)
    assert result["pr"] == 12
    assert result["version"] == item.version
    assert sessions.load(item.work)["phase"] == "planning"
    assert len(sessions.load(item.work)["handoffs"]) == 1
    assert preparation.prepare(item.root) == result


@pytest.mark.parametrize("check", ["fast-checks", "agent-review"])
def test_recovered_merge_requires_current_head_checks(publication, check: str):
    item = publication
    item.pr.update(state="MERGED", mergedAt="today")
    for status in item.pr["statusCheckRollup"]:
        if status["name"] == check:
            status["conclusion"] = "FAILURE"
    with pytest.raises(RuntimeError, match=check):
        preparation.prepare(item.root)
    assert preparation.load(item.root)["candidate_sha"] == "author"
    assert preparation.load(item.root)["pr"] == 12
    assert sessions.load(item.work)["handoffs"][-1]["owner"] == "pi"
    for status in item.pr["statusCheckRollup"]:
        status["conclusion"] = "SUCCESS"
    assert preparation.prepare(item.root)["status"] == "prepared"


@pytest.mark.parametrize(
    "problem",
    [
        "task",
        "digest",
        "branch",
        "base",
        "attachment",
        "closed",
        "ambiguous",
        "discovery",
        "parsing",
        "ancestry",
    ],
)
def test_invalid_discovery_never_republishes_or_records_release_pr(publication, problem: str):
    item = publication
    if problem in {"task", "digest"}:
        proto = task_context.protocol()
        context = proto.parse(item.pr["body"])
        if problem == "task":
            context["task_id"] = "other"
        else:
            context["starting_prompt"] = "Different request"
        item.pr["body"] = proto.render(context)
    elif problem in {"branch", "base"}:
        item.pr["headRefName" if problem == "branch" else "baseRefName"] = "other"
    elif problem == "attachment":
        item.pr["body"] = "Malformed attachment"
    elif problem == "closed":
        item.pr["state"] = "CLOSED"
    elif problem == "ambiguous":
        item.matches.append({"number": 13})
    elif problem == "discovery":
        item.faults["discovery"] = "authentication or transport failed"
    elif problem == "parsing":
        item.matches[:] = ["malformed"]
    elif problem == "ancestry":
        item.faults["ancestry"] = True
    with pytest.raises(
        (RuntimeError, ValueError, TypeError),
        match=r"task|attachment|closed|Ambiguous|failed|indices|ancestry",
    ):
        preparation.prepare(item.root)
    assert preparation.load(item.root)["pr"] is None
    assert sessions.load(item.work)["phase"] == "feature"


@pytest.mark.parametrize("problem", ["identifiers", "publication", "receipt"])
def test_conflicting_session_journal_blocks_recovery(publication, problem: str):
    item = publication
    session = sessions.load(item.work)
    if problem == "publication":
        session["publication_sha"] = "unrelated"
    else:
        session.update(
            pr=12,
            handoffs=[
                {
                    "pr": 13 if problem == "identifiers" else 12,
                    "branch": item.state["branch"],
                    "task_id": item.state["task_id"],
                    "task_digest": item.state["task_digest"],
                    "sha": "author" if problem == "identifiers" else "unrelated",
                    "owner": "pi",
                }
            ],
        )
    sessions.save(item.work, session)
    with pytest.raises(RuntimeError, match="conflicts|Conflicting"):
        preparation.prepare(item.root)
    assert preparation.load(item.root)["pr"] is None
    assert sessions.load(item.work)["phase"] == "feature"


def test_confirmed_absence_uses_existing_publication_once(
    publication,
    monkeypatch: pytest.MonkeyPatch,
):
    item = publication
    item.matches.clear()
    pushes = []

    def push(root):
        pushes.append(root)
        item.matches.append({"number": 12})
        session = sessions.load(root)
        session["pr"] = 12
        sessions.save(root, session)
        return sessions.handoff(root)

    monkeypatch.setattr(sessions, "push", push)
    result = preparation.prepare(item.root)
    assert result["pr"] == 12
    assert pushes == [item.work]
    assert preparation.prepare(item.root) == result
    assert pushes == [item.work]
