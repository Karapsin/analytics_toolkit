"""Display classification never changes merge authority or later owner decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.repository.github_agent import controller
from tests.repository.label_reconciliation import GitHub, insert_hold
from tests.repository.label_reconciliation import (
    merge_environment as merge_environment,  # noqa: PLC0414 - pytest fixture
)
from tests.repository.review_control import control as control  # noqa: PLC0414 - pytest fixture

if TYPE_CHECKING:
    from pathlib import Path


def hint(control: Any, gh: GitHub, required: bool = False) -> bool:
    return control.attention_hint(
        gh, gh.pr, control.attention_snapshot(gh, gh.pr), required, "d" * 64
    )


def test_internal_question_display_keeps_pause_and_survives_restart(
    control: Any, monkeypatch: Any
) -> None:
    gh = GitHub()
    insert_hold(control, "Correction agent requires user input.")
    before = control.attention_snapshot(gh, gh.pr)
    assert hint(control, gh)
    assert not control.owner_attention(gh, gh.pr)
    assert control.paused(gh, gh.pr)
    assert control.reconcile_label(gh, gh.pr, approval_required=False)
    assert not any(x["name"] == control.PAUSED for x in gh.pr["labels"])
    assert control.attention_snapshot(gh, gh.pr) == before
    with control.connect() as db:
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    monkeypatch.delitem(controller.MODULES, "review_control")
    restarted = controller.trusted_module("review_control")
    assert not restarted.owner_attention(gh, gh.pr)
    assert restarted.paused(gh, gh.pr)
    assert hint(control, gh, True)
    assert control.owner_attention(gh, gh.pr)
    assert control.reconcile_label(gh, gh.pr, approval_required=False)
    assert any(x["name"] == control.PAUSED for x in gh.pr["labels"])


@pytest.mark.parametrize("kind", ["question", "replacement", "veto", "security"])
def test_new_authority_overrides_older_display_hint(control: Any, kind: str) -> None:
    gh = GitHub()
    insert_hold(control, "Correction agent requires user input.")
    observed = control.attention_snapshot(gh, gh.pr)
    assert hint(control, gh)
    if kind == "question":
        control.hold_correction(gh, gh.pr)
    elif kind == "veto":
        with control.connect() as db:
            db.execute(
                "INSERT INTO vetoes VALUES (?,?,?,?)", (gh.repository, 1, gh.pr["head"]["sha"], 100)
            )
    else:
        insert_hold(
            control,
            "Supply original evidence."
            if kind == "security"
            else "Correction agent requires user input.",
        )
        with control.connect() as db:
            db.execute("UPDATE pauses SET created=created+1")
    assert control.owner_attention(gh, gh.pr)
    if kind != "veto":
        assert not control.attention_hint(gh, gh.pr, observed, False, "d" * 64)
    assert control.paused(gh, gh.pr)


def test_new_question_during_label_delete_wins(control: Any) -> None:
    gh = GitHub()
    insert_hold(control, "Correction agent requires user input.")
    assert hint(control, gh)
    gh.on_delete = lambda: control.hold_correction(gh, gh.pr)
    assert control.reconcile_label(gh, gh.pr, approval_required=False)
    assert any(x["name"] == control.PAUSED for x in gh.pr["labels"])
    assert control.paused(gh, gh.pr)


def test_renewed_planning_and_coordinator_authority_do_not_imply_owner_question(
    control: Any, monkeypatch: Any
) -> None:
    gh = GitHub()
    insert_hold(control, "Owner changed the request; renewed planning is required.")
    assert control.paused(gh, gh.pr)
    assert not control.owner_attention(gh, gh.pr)
    assert control.reconcile_label(gh, gh.pr, approval_required=False)
    assert not any(x["name"] == control.PAUSED for x in gh.pr["labels"])
    # A separate coordinator gate can block with no owner pause at all.
    with control.connect() as db:
        db.execute("DELETE FROM pauses")
    monkeypatch.setattr(control, "paused", lambda *args: True)
    assert control.reconcile_label(gh, gh.pr, approval_required=False)
    assert control.paused(gh, gh.pr)
    assert not control.owner_attention(gh, gh.pr)
    assert control.reconcile_label(gh, gh.pr, approval_required=True)
    assert any(x["name"] == control.PAUSED for x in gh.pr["labels"])


def test_question_without_pause_and_stale_heads_remain_independent(control: Any) -> None:
    gh = GitHub()
    assert hint(control, gh, True)
    assert control.owner_attention(gh, gh.pr)
    gh.pr["head"]["sha"] = "b" * 40
    assert not control.owner_attention(gh, gh.pr)
    insert_hold(control, "Correction agent requires user input.")
    assert not hint(control, gh)
    assert control.owner_attention(gh, gh.pr)
    assert not control.attention_hint(gh, gh.pr, None, False, "d" * 64)


def test_internal_display_retry_preserves_authority_and_unrelated_labels(
    control: Any, monkeypatch: Any
) -> None:
    gh = GitHub()
    insert_hold(control, "Correction agent requires user input.")
    observed = control.attention_snapshot(gh, gh.pr)
    assert hint(control, gh)
    gh.fail_delete = True
    with pytest.raises(RuntimeError, match="Transient label"):
        control.reconcile_label(gh, gh.pr, approval_required=False)
    assert control.attention_snapshot(gh, gh.pr) == observed
    assert control.paused(gh, gh.pr)
    monkeypatch.delitem(controller.MODULES, "review_control")
    restarted = controller.trusted_module("review_control")
    gh.fail_delete = False
    assert restarted.reconcile_label(gh, gh.pr, approval_required=False)
    assert gh.pr["labels"] == [{"name": "feature"}]
    assert restarted.attention_snapshot(gh, gh.pr) == observed
    assert restarted.paused(gh, gh.pr)
    # A successful retry is idempotent and never becomes an owner decision.
    count = len(gh.mutations)
    assert restarted.reconcile_label(gh, gh.pr, approval_required=False)
    assert len(gh.mutations) == count
    with restarted.connect() as db:
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


@pytest.mark.parametrize("replace_pause", [False, True])
def test_reopened_pr_rechecks_attention_without_clearing_private_hold(
    control: Any, replace_pause: bool
) -> None:
    gh = GitHub()
    insert_hold(control, "Correction agent requires user input.")
    assert hint(control, gh)
    gh.pr["state"] = "closed"
    assert not control.reconcile_label(gh, gh.pr, approval_required=False)
    assert not gh.mutations
    if replace_pause:
        control.hold_correction(gh, gh.pr)
    gh.pr["state"] = "open"
    assert control.reconcile_label(gh, gh.pr, approval_required=False)
    labels = {label["name"] for label in gh.pr["labels"]}
    assert (control.PAUSED in labels) is replace_pause
    assert "feature" in labels
    assert control.paused(gh, gh.pr)


def test_coordinator_dependency_hides_attention_but_refuses_green_merge(
    merge_environment: Any, tmp_path: Path
) -> None:
    gh = GitHub()
    gh.owner_approval()
    binding = {"request_id": "companion", "revision": 1, "repository": gh.repository}
    merge_environment.hold_coordinator(gh, gh.pr, binding)
    assert not merge_environment.owner_attention(gh, gh.pr)
    controller.merge_selected(gh, 17, tmp_path, pr_numbers={1})
    assert gh.pr["state"] == "open"
    assert gh.pr["labels"] == [{"name": "feature"}]
    assert not any(path.endswith("/merge") for path, _method in gh.mutations)
    assert merge_environment.coordinator_held(gh, gh.pr)
    assert merge_environment.paused(gh, gh.pr)


@pytest.mark.parametrize("existing_pause", [False, True])
def test_amendment_invalidates_display_hint_and_preserves_other_authority(
    control: Any, monkeypatch: Any, existing_pause: bool
) -> None:
    gh = GitHub()
    control.enroll_feature(gh.repository, gh.pr["head"]["ref"], "amended-digest")
    if existing_pause:
        insert_hold(control, "Correction agent requires user input.")
    before = control.attention_snapshot(gh, gh.pr)
    assert hint(control, gh, True)
    with control.connect() as db:
        db.execute(
            "INSERT INTO vetoes VALUES (?,?,?,?)",
            (gh.repository, gh.pr["number"], gh.pr["head"]["sha"], 100),
        )
    monkeypatch.setattr(control.time, "time", lambda: 3000)
    control.hold_feature(gh, gh.pr)
    with control.connect() as db:
        assert db.execute("SELECT count(*) FROM attention_hints").fetchone()[0] == 0
        assert db.execute("SELECT created FROM vetoes").fetchone()[0] == 100
        pending = db.execute("SELECT * FROM feature_amendments").fetchone()
        assert (pending is not None) is existing_pause
        if existing_pause:
            assert control.attention_snapshot(gh, gh.pr) == before
            assert pending["created"] == 3000
        else:
            assert control.attention_snapshot(gh, gh.pr)["reason"] == (
                "Owner changed the request; renewed planning is required."
            )
    monkeypatch.delitem(controller.MODULES, "review_control")
    restarted = controller.trusted_module("review_control")
    assert restarted.paused(gh, gh.pr)
    assert restarted.owner_attention(gh, gh.pr)  # The independent veto remains visible.


def test_internal_display_hint_cannot_merge_an_approved_green_pr(
    merge_environment: Any, tmp_path: Path
) -> None:
    gh = GitHub()
    gh.owner_approval()
    insert_hold(merge_environment, "Correction agent requires user input.")
    observed = merge_environment.attention_snapshot(gh, gh.pr)
    assert hint(merge_environment, gh)
    controller.merge_selected(gh, 17, tmp_path, pr_numbers={1})
    assert gh.pr["state"] == "open"
    assert gh.pr["labels"] == [{"name": "feature"}]
    assert not any(path.endswith("/merge") for path, _method in gh.mutations)
    assert merge_environment.attention_snapshot(gh, gh.pr) == observed
    assert merge_environment.paused(gh, gh.pr)
    with merge_environment.connect() as db:
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
