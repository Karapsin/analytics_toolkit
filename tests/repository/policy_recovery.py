"""Owner-policy recovery preserves decisions made during GitHub lookup."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import pytest

from tests.repository.github_agent import controller
from tests.repository.review_control import GitHub

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("replacement", ["pause", "veto", "both", "evidence", "cancel"])
def test_old_policy_comment_preserves_decision_during_lookup(
    tmp_path: Path, monkeypatch: Any, replacement: str
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    head, number = gh.pr["head"]["sha"], gh.pr["number"]
    original = (gh.repository, number, head, "Owner disapproved policy.", 1000)
    with controls.connect() as db:
        db.execute("INSERT INTO pauses VALUES (?,?,?,?,?)", original)
        db.execute("INSERT INTO vetoes VALUES (?,?,?,?)", (gh.repository, number, head, 1000))
    gh.api(f"issues/{number}/labels", "POST", {"labels": [controls.PAUSED]})
    gh.comments.append(
        {
            "body": "/agent approve-policy " + head,
            "user": {"type": "User", "login": "owner"},
            "created_at": datetime.fromtimestamp(2000, timezone.utc).isoformat(),
        }
    )
    reason = {
        "evidence": "Supply the original image.",
        "cancel": "Owner cancelled the request.",
    }.get(replacement, original[3])
    expected = original if replacement == "veto" else (*original[:3], reason, 3000)
    veto_created = 3000 if replacement in {"veto", "both"} else 1000
    installed = False

    def comments_with_new_decision(path: str) -> list[dict[str, Any]]:
        nonlocal installed
        assert "/comments?" in path
        if not installed:
            installed = True
            # The snapshot has been read and its transaction released. Insert
            # the newer authority at the real GitHub comment-lookup boundary.
            with controls.connect() as db:
                db.execute("INSERT OR REPLACE INTO pauses VALUES (?,?,?,?,?)", expected)
                db.execute(
                    "INSERT OR REPLACE INTO vetoes VALUES (?,?,?,?)",
                    (gh.repository, number, head, veto_created),
                )
        return gh.comments

    monkeypatch.setattr(gh, "pages", comments_with_new_decision)
    assert controls.paused(gh, gh.pr)
    assert installed
    monkeypatch.delitem(controller.MODULES, "review_control")
    restarted = controller.trusted_module("review_control")
    restarted.resume_correction(gh, gh.pr)
    restarted.recover_feature_publication(gh, gh.pr)
    assert restarted.paused(gh, gh.pr)
    with restarted.connect() as db:
        assert tuple(db.execute("SELECT * FROM pauses").fetchone()) == expected
        assert db.execute("SELECT created FROM vetoes").fetchone()[0] == veto_created
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    assert controls.PAUSED in {label["name"] for label in gh.pr["labels"]}


@pytest.mark.parametrize("veto_created", [0, 1000, 3000])
def test_policy_comment_retires_only_older_inspected_authority(
    tmp_path: Path, monkeypatch: Any, veto_created: int
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    head, number = gh.pr["head"]["sha"], gh.pr["number"]
    with controls.connect() as db:
        db.execute(
            "INSERT INTO pauses VALUES (?,?,?,?,?)",
            (gh.repository, number, head, "Owner disapproved policy.", 1000),
        )
        if veto_created:
            db.execute(
                "INSERT INTO vetoes VALUES (?,?,?,?)",
                (gh.repository, number, head, veto_created),
            )
    gh.comments.append(
        {
            "body": "/agent approve-policy " + head,
            "user": {"type": "User", "login": "owner"},
            "created_at": datetime.fromtimestamp(2000, timezone.utc).isoformat(),
        }
    )
    assert controls.paused(gh, gh.pr) is (veto_created == 3000)
    with controls.connect() as db:
        assert db.execute("SELECT count(*) FROM pauses").fetchone()[0] == (veto_created == 3000)
        assert db.execute("SELECT count(*) FROM vetoes").fetchone()[0] == (veto_created == 3000)
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
