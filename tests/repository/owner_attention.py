"""Display classification never changes merge authority or later owner decisions."""

from __future__ import annotations

from typing import Any

import pytest

from tests.repository.github_agent import controller
from tests.repository.label_reconciliation import GitHub, insert_hold
from tests.repository.review_control import control as control  # noqa: PLC0414 - pytest fixture


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
