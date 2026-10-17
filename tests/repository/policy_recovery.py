"""Owner-policy recovery preserves decisions made during GitHub lookup."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import checks, controller
from tests.repository.review_control import GitHub

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("caller", ["paused", "authorization", "merge"])
@pytest.mark.parametrize("replacement", ["pause", "veto", "both", "evidence", "cancel"])
def test_old_policy_comment_preserves_decision_during_lookup(
    tmp_path: Path, monkeypatch: Any, replacement: str, caller: str
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    monkeypatch.setattr(controller, "signed_head", lambda *_args: True)
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
    with controls.connect() as db:
        db.execute(
            "INSERT INTO approvals VALUES (?,?,?,?,?)",
            (gh.repository, number, head, 22, "prior-private-approval"),
        )
    installed = False

    def comments_with_new_decision(path: str) -> list[dict[str, Any]]:
        nonlocal installed
        if path.startswith("pulls/"):
            return [{"filename": ".github/agent/controller.py"}]
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
    if caller == "paused":
        assert controls.paused(gh, gh.pr)
    elif caller == "authorization":
        assert controller.authorization_error(gh, gh.pr) is not None
    else:
        gh.pulls = Mock(return_value=[gh.pr])
        gh.base = Mock(return_value="base")
        gh.checks = Mock(return_value=checks())
        gh.api = Mock(wraps=gh.api)
        controller.merge_selected(gh, 17, tmp_path, pr_numbers={number})
        assert not any(call.args[0].endswith("/merge") for call in gh.api.call_args_list)
    assert installed
    monkeypatch.delitem(controller.MODULES, "review_control")
    restarted = controller.trusted_module("review_control")
    restarted.resume_correction(gh, gh.pr)
    restarted.recover_feature_publication(gh, gh.pr)
    assert restarted.paused(gh, gh.pr)
    with restarted.connect() as db:
        assert tuple(db.execute("SELECT * FROM pauses").fetchone()) == expected
        assert db.execute("SELECT created FROM vetoes").fetchone()[0] == veto_created
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1
    assert controls.PAUSED in {label["name"] for label in gh.pr["labels"]}


@pytest.mark.parametrize("caller", ["paused", "authorization"])
@pytest.mark.parametrize("pause_created", [1000, 2000, 3000])
@pytest.mark.parametrize("veto_created", [0, 1000, 2000, 3000])
def test_policy_comment_retires_only_older_inspected_authority(
    tmp_path: Path, monkeypatch: Any, veto_created: int, pause_created: int, caller: str
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    monkeypatch.setattr(controller, "signed_head", lambda *_args: True)
    head, number = gh.pr["head"]["sha"], gh.pr["number"]
    with controls.connect() as db:
        db.execute(
            "INSERT INTO pauses VALUES (?,?,?,?,?)",
            (gh.repository, number, head, "Owner disapproved policy.", pause_created),
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
    blocked = max(pause_created, veto_created) >= 2000
    if caller == "paused":
        assert controls.paused(gh, gh.pr) is blocked
    else:
        comments = gh.pages
        monkeypatch.setattr(
            gh,
            "pages",
            lambda path: (
                [{"filename": ".github/agent/controller.py"}]
                if path.startswith("pulls/")
                else comments(path)
            ),
        )
        assert (controller.authorization_error(gh, gh.pr) is not None) is blocked
    with controls.connect() as db:
        assert db.execute("SELECT count(*) FROM pauses").fetchone()[0] == blocked
        expected_veto = blocked and bool(veto_created)
        assert db.execute("SELECT count(*) FROM vetoes").fetchone()[0] == expected_veto
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("caller", "initial"),
    [
        ("paused", "pause"),
        ("authorization", "pause"),
        ("authorization", "veto"),
        ("authorization", "neither"),
        ("paused", "both"),
        ("authorization", "both"),
    ],
)
def test_policy_lookup_requires_the_exact_snapshot(
    tmp_path: Path, monkeypatch: Any, caller: str, initial: str
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(controller, "signed_head", lambda *_args: True)
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    head, number = gh.pr["head"]["sha"], gh.pr["number"]
    pause = (gh.repository, number, head, "Owner disapproved policy.", 1000)
    veto = (gh.repository, number, head, 1000)
    with controls.connect() as db:
        if initial in {"pause", "both"}:
            db.execute("INSERT INTO pauses VALUES (?,?,?,?,?)", pause)
        if initial in {"veto", "both"}:
            db.execute("INSERT INTO vetoes VALUES (?,?,?,?)", veto)
        db.execute(
            "INSERT INTO approvals VALUES (?,?,?,?,?)",
            (gh.repository, number, head, 22, "prior-private-approval"),
        )

    if initial == "both":
        # The replacement remains older than the approval: identity alone must
        # reject this attempt without recursively recovering against a new read.
        pause = (*pause[:4], 1500)
        veto = (*veto[:3], 1500)

    def pages(path: str) -> list[dict[str, Any]]:
        if path.startswith("pulls/"):
            return [{"filename": ".github/agent/controller.py"}]
        # Even an older newly appearing row invalidates the exact snapshot.
        with controls.connect() as db:
            db.execute("INSERT OR REPLACE INTO pauses VALUES (?,?,?,?,?)", pause)
            db.execute("INSERT OR REPLACE INTO vetoes VALUES (?,?,?,?)", veto)
        return [
            {
                "body": "/agent approve-policy " + head,
                "user": {"type": "User", "login": "owner"},
                "created_at": datetime.fromtimestamp(2000, timezone.utc).isoformat(),
            }
        ]

    monkeypatch.setattr(gh, "pages", pages)
    if caller == "paused":
        assert controls.paused(gh, gh.pr)
    else:
        assert controller.authorization_error(gh, gh.pr) is not None
    with controls.connect() as db:
        assert tuple(db.execute("SELECT * FROM pauses").fetchone()) == pause
        assert tuple(db.execute("SELECT * FROM vetoes").fetchone()) == veto


@pytest.mark.parametrize("reason", ["Supply the original image.", "Owner cancelled the request."])
def test_policy_approval_preserves_an_existing_hold(
    tmp_path: Path, monkeypatch: Any, reason: str
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(controller, "signed_head", lambda *_args: True)
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    head, number = gh.pr["head"]["sha"], gh.pr["number"]
    pause = (gh.repository, number, head, reason, 1000)
    veto = (gh.repository, number, head, 1000)
    with controls.connect() as db:
        db.execute("INSERT INTO pauses VALUES (?,?,?,?,?)", pause)
        db.execute("INSERT INTO vetoes VALUES (?,?,?,?)", veto)
    gh.comments.append(
        {
            "body": "/agent approve-policy " + head,
            "user": {"type": "User", "login": "owner"},
            "created_at": datetime.fromtimestamp(2000, timezone.utc).isoformat(),
        }
    )
    monkeypatch.setattr(
        gh,
        "pages",
        lambda path: (
            [{"filename": ".github/agent/controller.py"}]
            if path.startswith("pulls/")
            else gh.comments
        ),
    )
    assert controller.authorization_error(gh, gh.pr) is not None
    assert controls.paused(gh, gh.pr)
    gh.pr["labels"] = []
    assert controls.reconcile_label(gh, gh.pr, approval_required=False)
    assert controls.PAUSED in {label["name"] for label in gh.pr["labels"]}
    with controls.connect() as db:
        assert tuple(db.execute("SELECT * FROM pauses").fetchone()) == pause
        assert tuple(db.execute("SELECT * FROM vetoes").fetchone()) == veto
