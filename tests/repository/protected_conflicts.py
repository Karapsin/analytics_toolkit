from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository._support.publication import (
    control as control,  # noqa: PLC0414 - pytest fixture
)
from tests.repository._support.publication import (
    publication as publication,  # noqa: PLC0414 - pytest fixture
)
from tests.repository.github_agent import controller

if TYPE_CHECKING:
    from pathlib import Path


def test_blocked_protected_merge_durably_routes_to_fresh_correction(
    publication: Any, control: Any, monkeypatch: Any
) -> None:
    work, git, _commit, task, artifact, gh, pushed, history = publication
    path = ".github/agent/controller.py"
    head, base, _ancestor = history(proposal=b"existing feature proposal\n")
    (work / path).write_bytes(b"resolution combining feature and dev\n")
    git("add", "--all")
    value = task(head, base)
    output = artifact(value)

    with pytest.raises(RuntimeError, match="fresh correction planning queued"):
        controller.publish(gh, value, output, work)
    assert not pushed
    assert git("rev-parse", "HEAD").decode().strip() == head
    request = control.correction(gh.repository, 1, head, value["task_digest"])
    assert request
    assert path in request["reason"]
    assert not control.pending()
    assert not control.paused(gh, gh.pr)
    assert not control.approved(gh.repository, 1, head)
    assert not control.correction(gh.repository, 1, "f" * 40, value["task_digest"])
    assert not control.correction(gh.repository, 1, head, "f" * 64)
    assert all("Reviewer requested changes" not in c["body"] for c in gh.comments)

    # A new controller instance must see the durable request after restart.
    monkeypatch.setattr(controller, "MODULES", {})
    reloaded = controller.trusted_module("review_control")
    assert reloaded.correction(gh.repository, 1, head, value["task_digest"]) == request
    original_api = gh.api

    def api(endpoint: str, method: str = "GET", data: Any = None) -> Any:
        if endpoint.startswith("collaborators/"):
            return {"permission": "write"}
        if endpoint.startswith("compare/"):
            return {"behind_by": 1}
        return original_api(endpoint, method, data)

    monkeypatch.setattr(gh, "api", api)
    monkeypatch.setattr(
        gh,
        "pages",
        lambda endpoint, key=None: gh.comments if endpoint.startswith("issues/") else [],
    )
    monkeypatch.setattr(gh, "pulls", Mock(return_value=[gh.pr]), raising=False)
    monkeypatch.setattr(gh, "checks", Mock(return_value=[]), raising=False)
    monkeypatch.setattr(
        controller,
        "monitor",
        lambda _gh: (
            1,
            {"started": "2026-01-01T00:00:00Z", "runs": {}, "failures": {}, "candidates": {}},
        ),
    )
    monkeypatch.setattr(controller, "save_monitor", Mock())
    tasks = controller.discover(gh, 17)["include"]
    assert len(tasks) == 1
    assert tasks[0]["kind"] == "correction"
    assert tasks[0]["head"] == head
    assert tasks[0]["base"] == base
    assert tasks[0]["task_digest"] == value["task_digest"]
    assert tasks[0]["user_task"] == value["user_task"]
    assert tasks[0]["rejection"] == request
    assert not reloaded.pending()


def test_sensitive_path_cannot_create_autonomous_protected_correction(
    publication: Any, control: Any
) -> None:
    work, git, _commit, task, artifact, gh, pushed, history = publication
    head, base, _ancestor = history(proposal=b"feature controller proposal\n")
    (work / ".github/agent/controller.py").write_bytes(b"protected resolution\n")
    (work / ".env").write_text("fake fixture configuration\n")
    git("add", "--all")
    value = task(head, base)
    output = artifact(value)
    with pytest.raises(RuntimeError, match="controller or sensitive"):
        controller.publish(gh, value, output, work)
    assert not pushed
    assert not control.correction(gh.repository, 1, head, value["task_digest"])
    assert not control.pending()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("changed", ["head", "digest", "findings"])
def test_inflight_conflict_cannot_replace_concurrent_correction(
    publication: Any, control: Any, monkeypatch: Any, existing: bool, changed: str
) -> None:
    work, git, _commit, task, artifact, gh, pushed, history = publication
    head, base, _ancestor = history(proposal=b"feature controller proposal\n")
    (work / ".github/agent/controller.py").write_bytes(b"protected resolution\n")
    git("add", "--all")
    value = task(head, base)
    output = artifact(value)
    if existing:
        control.require_correction(
            gh.repository,
            1,
            "b" * 40,
            "e" * 64,
            "Old reason",
            "Old assessment",
            expected=None,
        )
    newer_head = "f" * 40 if changed == "head" else head
    newer_digest = "f" * 64 if changed == "digest" else value["task_digest"]
    inherited = controller.inherited_controller_path

    def inspect(*args: Any) -> bool:
        with control.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO corrections VALUES (?,?,?,?,?,?,?)",
                (
                    gh.repository,
                    1,
                    newer_head,
                    newer_digest,
                    "New findings",
                    "New assessment",
                    99.0,
                ),
            )
        return inherited(*args)

    monkeypatch.setattr(controller, "inherited_controller_path", inspect)
    message = "fresh correction planning queued" if changed == "findings" else "findings changed"
    with pytest.raises(RuntimeError, match=message):
        controller.publish(gh, value, output, work)
    assert control.correction(gh.repository, 1, newer_head, newer_digest) == {
        "reason": "New findings",
        "changes": "New assessment",
    }
    assert not pushed
    assert not control.pending()


@pytest.mark.parametrize("change", ["head", "hold"])
def test_routing_rechecks_authority_after_protected_history_inspection(
    publication: Any, control: Any, monkeypatch: Any, change: str
) -> None:
    work, git, _commit, task, artifact, gh, pushed, history = publication
    head, base, _ancestor = history(proposal=b"feature controller proposal\n")
    (work / ".github/agent/controller.py").write_bytes(b"protected resolution\n")
    git("add", "--all")
    value = task(head, base)
    output = artifact(value)
    inherited = controller.inherited_controller_path

    def inspect(*args: Any) -> bool:
        if change == "head":
            gh.pr["head"]["sha"] = "f" * 40
        else:
            with control.connect() as db:
                db.execute(
                    "INSERT INTO pauses VALUES (?,?,?,?,?)",
                    (gh.repository, 1, head, "Owner/security hold", 1.0),
                )
        return inherited(*args)

    monkeypatch.setattr(controller, "inherited_controller_path", inspect)
    message = "PR changed" if change == "head" else "awaiting user"
    with pytest.raises(RuntimeError, match=message):
        controller.publish(gh, value, output, work)
    assert not control.correction_snapshot(gh.repository, 1)
    assert not pushed
    assert not control.pending()


@pytest.mark.parametrize("stale", [None, "head", "digest"])
def test_routing_preserves_current_findings_replaces_stale_scope_and_retains_hold(
    tmp_path: Path, monkeypatch: Any, stale: str | None
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    old_head = "b" * 40 if stale == "head" else "a" * 40
    old_digest = "e" * 64 if stale == "digest" else "d" * 64
    controls.require_correction(
        "owner/repo",
        1,
        old_head,
        old_digest,
        "Existing source findings",
        "Original assessment",
        expected=None,
    )
    with controls.connect() as db:
        db.execute(
            "INSERT INTO pauses VALUES (?,?,?,?,?)",
            ("owner/repo", 1, "a" * 40, "Owner/security hold", 1.0),
        )
    controls.require_correction(
        "owner/repo",
        1,
        "a" * 40,
        "d" * 64,
        "Protected merge requires planning",
        "Routing assessment",
        expected=controls.correction_snapshot("owner/repo", 1),
    )
    expected = (
        {"reason": "Protected merge requires planning", "changes": "Routing assessment"}
        if stale
        else {"reason": "Existing source findings", "changes": "Original assessment"}
    )
    assert controls.correction("owner/repo", 1, "a" * 40, "d" * 64) == expected
    if stale:
        assert not controls.correction("owner/repo", 1, old_head, old_digest)
    with controls.connect() as db:
        row = db.execute("SELECT reason,created FROM pauses").fetchone()
        assert tuple(row) == ("Owner/security hold", 1.0)
    assert not controls.pending()
    assert not controls.approved("owner/repo", 1, "a" * 40)
