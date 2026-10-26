"""Feature base revalidation and verified visual evidence for the local reviewer."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from agent_tools import local_review, sessions, sql_explorer_visual, task_context
from tests.agent_tools._support.local_review import signed_receipt
from tests.agent_tools.handoff import no_network, submitted
from tests.agent_tools.sessions import git
from tests.agent_tools.sessions import source as source  # noqa: PLC0414

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def candidate(source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root, pr = submitted(source, tmp_path)
    no_network(monkeypatch, pr)
    monkeypatch.setattr(local_review, "validation", lambda root: {"ok": True, "fixture": True})
    return root


def commit(root: Path, files: dict[str, str], message: str = "Change") -> str:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(root, "add", name)
    git(
        root,
        *sessions.commit_options(root),
        "commit",
        "-m",
        message + task_context.commit_trailers(root),
    )
    return git(root, "rev-parse", "HEAD")


def advance_dev(source: Path, name: str = "other-pr") -> str:
    git(source, "pull", "--ff-only", "origin", "dev")
    (source / name).write_text("another PR\n")
    git(source, "add", name)
    git(source, "commit", "-m", "Another merged PR")
    git(source, "push", "origin", "dev")
    return git(source, "rev-parse", "HEAD")


def merge_dev(root: Path) -> str:
    git(root, "fetch", "origin", "dev")
    git(
        root,
        *sessions.commit_options(root),
        "merge",
        "--no-ff",
        "origin/dev",
        "-m",
        "Merge dev" + task_context.commit_trailers(root),
    )
    return git(root, "rev-parse", "HEAD")


def capture_review(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def execute(root: Path, job: Path, subject: dict[str, str], request: str) -> dict[str, Any]:
        calls.append({"subject": subject, "request": json.loads(request)})
        return {"payload": subject}

    monkeypatch.setattr(local_review, "_execute_review", execute)
    return calls


def test_merged_dev_advances_base_with_revalidation_receipt(candidate, source) -> None:
    before = sessions.load(candidate)["base"]
    dev = advance_dev(source)
    head = merge_dev(candidate)
    receipt = sessions.advance_base(candidate)
    assert receipt is not None
    assert (receipt["before"], receipt["after"], receipt["head"]) == (before, dev, head)
    assert "other-pr" in receipt["changes"]
    state = sessions.load(candidate)
    assert state["base"] == dev
    assert state["base_revalidations"] == [receipt]
    assert local_review.binding(candidate, head)["base"] == dev
    assert sessions.advance_base(candidate) is None
    assert sessions.load(candidate)["base_revalidations"] == [receipt]


def test_base_never_moves_to_dev_commits_missing_from_head(candidate, source) -> None:
    before = sessions.load(candidate)["base"]
    advance_dev(source)
    assert sessions.advance_base(candidate) is None
    assert sessions.load(candidate)["base"] == before
    assert "base_revalidations" not in sessions.load(candidate)


def test_base_never_moves_backwards_or_off_the_recorded_history(candidate, source) -> None:
    dev = advance_dev(source)
    merge_dev(candidate)
    state = sessions.load(candidate)
    state["base"] = dev
    sessions.save(candidate, state)
    # A later commit unknown to HEAD cannot become the base and the base never regresses.
    advance_dev(source, "later")
    assert sessions.advance_base(candidate) is None
    assert sessions.load(candidate)["base"] == dev


def test_refresh_advances_base_after_a_dev_merge(candidate, source) -> None:
    dev = advance_dev(source)
    merge_dev(candidate)
    result = sessions.refresh(candidate)
    assert result["base"] == dev
    assert result["base_revalidation"]["after"] == dev


def test_review_diffs_against_merged_dev_and_supersedes_stale_receipt(
    candidate, source, monkeypatch
) -> None:
    calls = capture_review(monkeypatch)
    dev = advance_dev(source)
    head = merge_dev(candidate)
    stale = signed_receipt(candidate)  # Bound to the pre-merge base for this exact head.
    assert stale["payload"]["base"] != dev
    local_review.review(candidate)
    (call,) = calls
    assert call["subject"]["base"] == dev
    assert call["request"]["base_revalidations"][0]["after"] == dev
    changed = git(candidate, "diff", "--name-only", call["subject"]["base"], head)
    assert "other-pr" not in changed.split()
    reviews = local_review.directory(candidate)
    assert not (reviews / (head + ".json")).exists()
    assert list(reviews.glob(head + ".superseded-*.json"))


def visual_candidate(root: Path) -> None:
    manifest = {"schema_version": 1, "scenes": [{"id": "main"}, {"id": "picker"}]}
    commit(
        root,
        {
            ".gitignore": ".rag_index/\n",
            str(sql_explorer_visual.MANIFEST): json.dumps(manifest),
            "analytics_toolkit/sql_explorer/app.py": "APP = 1\n",
        },
        "SQL Explorer change",
    )


def complete_visual_review(root: Path) -> dict[str, Any]:
    """Drive the real producer; only the headless capture itself is simulated."""
    session = sql_explorer_visual.start_review(root, "review-0001")
    session["capture"] = {"status": "pass", "workspace_removed": True}
    for index, scene in enumerate(session["scenes"].values()):
        scene.update(
            capture="pass",
            screenshot_sha256=f"{index:064x}",
            geometry_sha256=f"{index + 10:064x}",
            verdict="pass",
            notes="Reviewed full PNG",
        )
    path = sql_explorer_visual.session_path(root, "review-0001")
    path.write_text(json.dumps(session))
    sql_explorer_visual.complete_review(root, "review-0001")
    return json.loads((root / sql_explorer_visual.RECEIPT).read_text())


def write_visual_receipt(root: Path, **changes: Any) -> dict[str, Any]:
    receipt = {**complete_visual_review(root), **changes}
    (root / sql_explorer_visual.RECEIPT).write_text(json.dumps(receipt))
    return receipt


def test_review_attaches_current_visual_receipt_bound_to_task(candidate, monkeypatch) -> None:
    calls = capture_review(monkeypatch)
    visual_candidate(candidate)
    receipt = complete_visual_review(candidate)
    spoof = sessions.state_path(candidate).with_name("atk-local-review-evidence.json")
    digest = local_review.binding(candidate, git(candidate, "rev-parse", "HEAD"))["task_digest"]
    spoof.write_text(
        json.dumps({"task_digest": digest, local_review.VISUAL_EVIDENCE_KEY: {"forged": True}})
    )
    local_review.review(candidate)
    (call,) = calls
    evidence = call["request"]["evidence"]
    assert evidence["task_digest"] == call["subject"]["task_digest"]
    visual = evidence[local_review.VISUAL_EVIDENCE_KEY]
    assert visual["verified"] is True
    assert visual["receipt"] == receipt
    assert visual["task_digest"] == call["subject"]["task_digest"]
    assert (visual["head"], visual["tree"]) == (
        call["subject"]["head"],
        call["subject"]["tree"],
    )
    assert "analytics_toolkit/sql_explorer/app.py" in visual["changed_visual_paths"]
    data = (candidate / sql_explorer_visual.RECEIPT).read_bytes()
    assert visual["receipt_sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"content_fingerprint": "0" * 64}, "stale"),
        ({"scenes": {"main": {"verdict": "pass"}}}, "incomplete"),
        ({"capture_mode": "vm"}, "obsolete"),
        (None, "No complete"),
    ],
)
def test_review_rejects_missing_stale_or_partial_visual_receipt(
    candidate, monkeypatch, changes, message
) -> None:
    calls = capture_review(monkeypatch)
    visual_candidate(candidate)
    if changes is not None:
        write_visual_receipt(candidate, **changes)
    with pytest.raises(RuntimeError, match=message):
        local_review.review(candidate)
    assert calls == []


def test_non_visual_change_never_attaches_or_accepts_visual_evidence(
    candidate, monkeypatch
) -> None:
    calls = capture_review(monkeypatch)
    spoof = sessions.state_path(candidate).with_name("atk-local-review-evidence.json")
    digest = local_review.binding(candidate, git(candidate, "rev-parse", "HEAD"))["task_digest"]
    spoof.write_text(
        json.dumps(
            {"task_digest": digest, "scope": "Approved", local_review.VISUAL_EVIDENCE_KEY: {}}
        )
    )
    (local_review.directory(candidate) / (git(candidate, "rev-parse", "HEAD") + ".json")).unlink()
    local_review.review(candidate)
    (call,) = calls
    assert call["request"]["evidence"] == {"task_digest": digest, "scope": "Approved"}


def scene_without_geometry(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        name: {"verdict": "pass", "screenshot_sha256": scene["screenshot_sha256"]}
        for name, scene in receipt["scenes"].items()
    }


@pytest.mark.parametrize("defect", ["metadata", "geometry", "session", "mismatch"])
def test_review_rejects_receipts_not_produced_by_a_completed_review(
    candidate, monkeypatch, defect
) -> None:
    calls = capture_review(monkeypatch)
    visual_candidate(candidate)
    receipt = complete_visual_review(candidate)
    session = sql_explorer_visual.session_path(candidate, "review-0001")
    if defect == "metadata":
        receipt.pop("completed_at_epoch")
    elif defect == "geometry":
        receipt["scenes"] = scene_without_geometry(receipt)
    elif defect == "session":
        session.unlink()
    else:
        recorded = json.loads(session.read_text())
        recorded["capture"]["workspace_removed"] = False
        session.write_text(json.dumps(recorded))
    (candidate / sql_explorer_visual.RECEIPT).write_text(json.dumps(receipt))
    # The existing gate accepts these; the reviewer attachment must not.
    assert sql_explorer_visual.verify_visual_receipt(
        candidate, paths=["analytics_toolkit/sql_explorer/app.py"]
    )["ok"]
    with pytest.raises(RuntimeError, match="not a completed visual review"):
        local_review.review(candidate)
    assert calls == []


def test_quoted_non_ascii_sql_explorer_paths_still_require_the_receipt(
    candidate, monkeypatch
) -> None:
    calls = capture_review(monkeypatch)
    git(candidate, "config", "core.quotePath", "true")
    commit(
        candidate,
        {
            ".gitignore": ".rag_index/\n",
            "analytics_toolkit/sql_explorer/r\u00e9sum\u00e9.py": "VALUE = 1\n",
        },
    )
    with pytest.raises(RuntimeError, match="No complete"):
        local_review.review(candidate)
    assert calls == []
