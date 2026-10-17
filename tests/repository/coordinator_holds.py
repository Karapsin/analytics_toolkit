"""Dependency waits retain controller authority independently of intake scheduling."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import pytest

from tests.repository.agent_lifecycle import _discovery_client
from tests.repository.github_agent import controller
from tests.repository.review_control import GitHub

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("owner_resume", [False, True])
def test_dependency_wait_blocks_green_head_after_restart(
    tmp_path: Path, monkeypatch: Any, owner_resume: bool
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    binding = {
        "request_id": "ancestry-request",
        "revision": 115,
        "repository": gh.repository,
        "state_sha256": "retained-questions-and-plan",
        "checkpoint_sha256": "retained-patch",
    }
    # No preexisting pause: intake alone previously left this head mergeable.
    controls.hold_coordinator(gh, gh.pr, binding)
    if owner_resume:
        gh.comments.append(
            {
                "body": "/agent resume " + gh.pr["head"]["sha"],
                "user": {"type": "User", "login": gh.repository.split("/")[0]},
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    monkeypatch.delitem(controller.MODULES, "review_control")
    restarted = controller.trusted_module("review_control")
    assert restarted.paused(gh, gh.pr)
    restarted.resume_correction(gh, gh.pr)
    restarted.recover_feature_publication(gh, gh.pr)
    assert restarted.paused(gh, gh.pr)
    client, metadata = _discovery_client(gh, monkeypatch)
    monkeypatch.setattr(controller, "ready", lambda *args: True)
    assert controller.discover(client, 17) == {"include": []}
    controller.merge_selected(client, 17, tmp_path, pr_numbers={gh.pr["number"]})
    metadata.assert_not_called()
    assert not any(call.args[0].endswith("/merge") for call in client.api.call_args_list)
    with restarted.connect() as db:
        assert db.execute("SELECT count(*) FROM pauses").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    # A stale receipt cannot retire a changed dependency, even on the same head.
    newer = {**binding, "revision": 116}
    restarted.hold_coordinator(gh, gh.pr, newer)
    restarted.hold_coordinator(gh, gh.pr, binding)  # Stale reconciliation after restart.
    assert not restarted.release_coordinator(gh, gh.pr, binding)
    assert restarted.paused(gh, gh.pr)
    assert restarted.release_coordinator(gh, gh.pr, newer)
    assert not restarted.paused(gh, gh.pr)


def test_verified_dependency_cannot_retire_other_requests_or_owner_authority(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    binding = {"request_id": "first", "revision": 1, "repository": gh.repository}
    controls.hold_coordinator(gh, gh.pr, binding)
    other = {**binding, "request_id": "second"}
    controls.hold_coordinator(gh, gh.pr, other)
    with controls.connect() as db:
        db.execute(
            "INSERT INTO pauses VALUES (?,?,?,?,?)",
            (
                gh.repository,
                gh.pr["number"],
                gh.pr["head"]["sha"],
                "Supply the original image.",
                1000,
            ),
        )
        original = tuple(db.execute("SELECT * FROM pauses").fetchone())
    assert controls.release_coordinator(gh, gh.pr, binding)
    assert controls.coordinator_held(gh, gh.pr)
    assert controls.release_coordinator(gh, gh.pr, other)
    assert controls.paused(gh, gh.pr)
    with controls.connect() as db:
        assert tuple(db.execute("SELECT * FROM pauses").fetchone()) == original
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


@pytest.mark.parametrize("restart", [False, True])
def test_amendment_publication_capability_cannot_bypass_dependency(
    tmp_path: Path, monkeypatch: Any, restart: bool
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    controls.enroll_feature(gh.repository, gh.pr["head"]["ref"], "new-digest")
    controls.hold_feature(gh, gh.pr)
    with controls.connect() as db:
        amendment = dict(db.execute("SELECT * FROM pauses").fetchone())
    binding = {"request_id": "companion", "revision": 3, "repository": gh.repository}
    controls.hold_coordinator(gh, gh.pr, binding)
    if restart:
        monkeypatch.delitem(controller.MODULES, "review_control")
        controls = controller.trusted_module("review_control")
    client, _ = _discovery_client(gh, monkeypatch)
    task = {
        "kind": "feature",
        "number": gh.pr["number"],
        "head": gh.pr["head"]["sha"],
        "base": "base",
        "branch": gh.pr["head"]["ref"],
        "task_digest": "new-digest",
        "previous_task_digest": "digest",
    }
    with pytest.raises(RuntimeError, match="coordinator dependency"):
        controller.publication_authority(client, task, renewal_hold=amendment)
    assert controls.coordinator_held(gh, gh.pr)
    assert controls.release_coordinator(gh, gh.pr, binding)
    assert controller.publication_authority(client, task, renewal_hold=amendment) == gh.pr
    assert controls.paused(gh, gh.pr)
    with controls.connect() as db:
        # Releasing the dependency grants publication capability only; the exact
        # amendment must still gate discovery/merge until successor verification.
        assert dict(db.execute("SELECT * FROM pauses").fetchone()) == amendment
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "other/repo"),
        ("request_id", ""),
        ("revision", "115"),
        ("revision", -1),
    ],
)
def test_invalid_dependency_identity_cannot_change_controller_state(
    tmp_path: Path, monkeypatch: Any, field: str, value: Any
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    binding = {"request_id": "dependency", "revision": 115, "repository": gh.repository}
    binding[field] = value
    with pytest.raises(ValueError, match="identity differs"):
        controls.hold_coordinator(gh, gh.pr, binding)
    assert not controls.coordinator_held(gh, gh.pr)


@pytest.mark.parametrize("interruption", ["unclaimed", "restart"])
def test_verified_dependency_handoff_blocks_old_green_review(
    tmp_path: Path, monkeypatch: Any, interruption: str
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    binding = {"request_id": "ancestry", "revision": 115, "repository": gh.repository}
    controls.hold_coordinator(gh, gh.pr, binding)
    assert controls.renew_coordinator(gh, gh.pr, binding, "digest")
    if interruption == "restart":
        monkeypatch.delitem(controller.MODULES, "review_control")
        controls = controller.trusted_module("review_control")
    assert not controls.coordinator_held(gh, gh.pr)
    assert not controls.paused(gh, gh.pr)  # Fresh planning/publication can run.
    assert controls.correction(gh.repository, gh.pr["number"], gh.pr["head"]["sha"], "digest")
    client, _ = _discovery_client(gh, monkeypatch)
    monkeypatch.setattr(controller, "ready", lambda *args: True)  # Earlier review/CI are green.
    controller.merge_selected(client, 17, tmp_path, pr_numbers={gh.pr["number"]})
    assert not any(call.args[0].endswith("/merge") for call in client.api.call_args_list)
    with controls.connect() as db:
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    # A successor head still needs its own review/CI, but does not inherit the
    # old head's renewal gate. Failed/lost publication leaves the old head gated.
    assert not controls.correction(gh.repository, gh.pr["number"], "b" * 40, "digest")


@pytest.mark.parametrize("case", ["replacement", "veto", "other-correction", "existing-findings"])
def test_dependency_handoff_preserves_replacement_authority(
    tmp_path: Path, monkeypatch: Any, case: str
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    binding = {"request_id": "first", "revision": 1, "repository": gh.repository}
    controls.hold_coordinator(gh, gh.pr, binding)
    if case == "replacement":
        controls.hold_coordinator(gh, gh.pr, {**binding, "revision": 2})
    elif case == "veto":
        with controls.connect() as db:
            db.execute(
                "INSERT INTO vetoes VALUES (?,?,?,?)",
                (gh.repository, gh.pr["number"], gh.pr["head"]["sha"], 1),
            )
    else:
        controls.require_correction(
            gh.repository,
            gh.pr["number"],
            gh.pr["head"]["sha"],
            "other-digest" if case == "other-correction" else "digest",
            "Original findings",
            "Complete feature",
            expected=None,
        )
    before = controls.correction_snapshot(gh.repository, gh.pr["number"])
    renewed = controls.renew_coordinator(gh, gh.pr, binding, "digest")
    assert renewed == (case in {"other-correction", "existing-findings"})
    assert controls.coordinator_held(gh, gh.pr) == (not renewed)
    assert controls.correction_snapshot(gh.repository, gh.pr["number"]) == before
