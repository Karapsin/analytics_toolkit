"""Exact dependency scopes permit reading without granting a writer or merge."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from tests.repository.github_agent import controller
from tests.repository.review_control import GitHub

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def scope(tmp_path: Path, monkeypatch: Any) -> tuple[Any, GitHub, dict[str, Any]]:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    gh = GitHub()
    binding = {
        "repository": gh.repository,
        "request_id": "dependent-request",
        "revision": 4,
        "candidate_head": gh.pr["head"]["sha"],
        "task_digest": "d" * 64,
        "plan_digest": "complete-conditional-plan",
        "prerequisite_digest": "reviewed-prerequisites",
    }
    controls.hold_coordinator(gh, gh.pr, binding)
    return controls, gh, binding


def snapshot(controls: Any) -> list[Any]:
    with closing(controls.connect()) as db, db:
        return list(db.iterdump())


@pytest.mark.parametrize("stage", ["plan", "plan-review", "plan-resolver"])
def test_reading_does_not_process_resumes_or_change_any_authority(
    scope: tuple[Any, GitHub, dict[str, Any]], stage: str
) -> None:
    controls, gh, binding = scope
    gh.comments.append({"body": "/agent resume " + gh.pr["head"]["sha"]})
    before = snapshot(controls)
    assert controls.readonly_coordinator_scope(gh, gh.pr, binding, stage=stage)
    assert snapshot(controls) == before
    assert controls.paused(gh, gh.pr)
    controls.resume_correction(gh, gh.pr)
    controls.recover_feature_publication(gh, gh.pr)
    assert controls.paused(gh, gh.pr)
    assert not controls.approved(gh.repository, gh.pr["number"], gh.pr["head"]["sha"])


@pytest.mark.parametrize("stage", ["code", "publish", "review", "merge", "validate", ""])
def test_scope_never_grants_a_writer_or_other_role(
    scope: tuple[Any, GitHub, dict[str, Any]], stage: str
) -> None:
    controls, gh, binding = scope
    assert not controls.readonly_coordinator_scope(gh, gh.pr, binding, stage=stage)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "foreign/repo"),
        ("request_id", "other"),
        ("revision", 3),
        ("candidate_head", "b" * 40),
        ("task_digest", ""),
        ("plan_digest", "other"),
        ("prerequisite_digest", "other"),
    ],
)
def test_one_changed_binding_field_never_excludes_a_hold(
    scope: tuple[Any, GitHub, dict[str, Any]], field: str, value: Any
) -> None:
    controls, gh, binding = scope
    assert not controls.readonly_coordinator_scope(
        gh, gh.pr, {**binding, field: value}, stage="plan"
    )


@pytest.mark.parametrize("other", ["coordinator", "pause", "veto", "amendment", "closed", "absent"])
def test_unrelated_authorities_and_absent_hold_still_deny_readonly_scope(
    scope: tuple[Any, GitHub, dict[str, Any]], other: str
) -> None:
    controls, gh, binding = scope
    if other == "coordinator":
        controls.hold_coordinator(gh, gh.pr, {**binding, "request_id": "another-request"})
    elif other == "closed":
        gh.pr["state"] = "closed"
    elif other == "absent":
        assert controls.release_coordinator(gh, gh.pr, binding)
    else:
        with closing(controls.connect()) as db, db:
            parameters = (gh.repository, gh.pr["number"], gh.pr["head"]["sha"])
            if other == "pause":
                db.execute(
                    "INSERT INTO pauses VALUES (?,?,?,?,?)", (*parameters, "Provenance failed", 1)
                )
            elif other == "veto":
                db.execute("INSERT INTO vetoes VALUES (?,?,?,?)", (*parameters, 1))
            else:
                db.execute("INSERT INTO feature_amendments VALUES (?,?,?,?)", (*parameters, 1))
    before = snapshot(controls)
    assert not controls.readonly_coordinator_scope(gh, gh.pr, binding, stage="plan-review")
    assert snapshot(controls) == before


def replacement(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        **binding,
        "revision": binding["revision"] + 1,
        "plan_digest": "new-reviewed-plan",
        "coordinator_predecessor_sha256": hashlib.sha256(
            json.dumps(binding, sort_keys=True).encode()
        ).hexdigest(),
    }


def test_atomic_inventory_replacement_replays_without_reinstalling_retired_hold(
    scope: tuple[Any, GitHub, dict[str, Any]],
) -> None:
    controls, gh, old = scope
    new = replacement(old)
    assert controls.replace_coordinator(gh, gh.pr, old, new)
    assert controls.replace_coordinator(gh, gh.pr, old, new)
    assert not controls.readonly_coordinator_scope(gh, gh.pr, old, stage="plan")
    assert controls.readonly_coordinator_scope(gh, gh.pr, new, stage="plan")
    assert controls.paused(gh, gh.pr)
    assert controls.renew_coordinator(gh, gh.pr, new, old["task_digest"])
    assert not controls.replace_coordinator(gh, gh.pr, old, new)
    with closing(controls.connect()) as db, db:
        assert db.execute("SELECT COUNT(*) FROM coordinator_holds").fetchone()[0] == 0
        assert (
            db.execute("SELECT digest FROM coordinator_renewals").fetchone()[0]
            == old["task_digest"]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "foreign/repo"),
        ("request_id", "other"),
        ("revision", 4),
        ("revision", True),
        ("candidate_head", "b" * 40),
        ("task_digest", "private-new-context"),
        ("coordinator_predecessor_sha256", "wrong"),
    ],
)
def test_inventory_cas_rejects_changed_authority_and_keeps_original(
    scope: tuple[Any, GitHub, dict[str, Any]], field: str, value: Any
) -> None:
    controls, gh, old = scope
    before = snapshot(controls)
    assert not controls.replace_coordinator(gh, gh.pr, old, {**replacement(old), field: value})
    assert snapshot(controls) == before


def test_other_requests_veto_and_same_revision_replacement_survive_stale_cas(
    scope: tuple[Any, GitHub, dict[str, Any]],
) -> None:
    controls, gh, old = scope
    other = {**old, "request_id": "other-request"}
    controls.hold_coordinator(gh, gh.pr, other)
    same_revision = {**old, "plan_digest": "a-different-live-intent"}
    controls.hold_coordinator(gh, gh.pr, same_revision)
    with closing(controls.connect()) as db, db:
        db.execute(
            "INSERT INTO vetoes VALUES (?,?,?,?)",
            (gh.repository, gh.pr["number"], gh.pr["head"]["sha"], 1),
        )
    before = snapshot(controls)
    assert not controls.replace_coordinator(gh, gh.pr, old, replacement(old))
    assert snapshot(controls) == before
    assert controls.coordinator_held(gh, gh.pr)
