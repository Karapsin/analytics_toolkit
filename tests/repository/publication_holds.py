from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from tests.repository._support.publication import GitHub
from tests.repository._support.publication import (
    control as control,  # noqa: PLC0414 - pytest fixture export
)
from tests.repository.github_agent import controller

if TYPE_CHECKING:
    from collections.abc import Iterator

CORRECTION = "Correction agent requires user input."
BINDING = "PR task context is not bound to its signed head commit."
ENROLLMENT = "Feature context does not match the host-owned request."


def saved_pause(control: Any, gh: GitHub) -> dict[str, Any] | None:
    with control.connect() as db:
        row = db.execute(
            "SELECT * FROM pauses WHERE repository=? AND number=?",
            (gh.repository, gh.pr["number"]),
        ).fetchone()
    return dict(row) if row else None


def setup_feature(control: Any, reason: str) -> GitHub:
    gh = GitHub()
    gh.pr["head"]["ref"] = (
        "codex/orchestrator-recovery" if reason == ENROLLMENT else "codex/telegram-feature-recovery"
    )
    if reason == CORRECTION:
        gh.pr["head"]["ref"] = "codex/local-correction"
    else:
        control.enroll_feature(gh.repository, gh.pr["head"]["ref"], "a" * 64)
    with control.connect() as db:
        db.execute(
            "INSERT INTO pauses VALUES (?,?,?,?,?)",
            (gh.repository, gh.pr["number"], gh.pr["head"]["sha"], reason, 1.0),
        )
    gh.pr["labels"] = [{"name": control.PAUSED}]
    return gh


@pytest.mark.parametrize("reason", [BINDING, ENROLLMENT])
@pytest.mark.parametrize("replacement", ["evidence", "cancel", "head", "timestamp", "veto"])
def test_recovery_cannot_delete_a_replacement_hold_or_late_veto(
    control: Any, monkeypatch: pytest.MonkeyPatch, reason: str, replacement: str
) -> None:
    gh = setup_feature(control, reason)
    original = saved_pause(control, gh)
    assert original is not None
    retained = dict(original)
    if replacement == "evidence":
        retained["reason"] = "Original image evidence is unavailable."
    elif replacement == "cancel":
        retained["reason"] = "Owner cancelled the request."
    elif replacement == "head":
        retained["head"] = "b" * 40
    if replacement != "veto":
        retained["created"] = 2.0
    enrolled = control.enrolled_feature
    injected = []

    def concurrent_decision(*args: Any) -> bool:
        eligible = enrolled(*args)
        with control.connect() as db:
            if replacement == "veto":
                db.execute(
                    "INSERT INTO vetoes VALUES (?,?,?,?)",
                    (gh.repository, gh.pr["number"], gh.pr["head"]["sha"], 2.0),
                )
            else:
                db.execute(
                    "INSERT OR REPLACE INTO pauses VALUES (?,?,?,?,?)",
                    tuple(
                        retained[key]
                        for key in ("repository", "number", "head", "reason", "created")
                    ),
                )
        injected.append(True)
        return eligible

    monkeypatch.setattr(control, "enrolled_feature", concurrent_decision)
    control.recover_feature_publication(gh, gh.pr, task_digest="a" * 64)
    assert injected == [True]
    assert saved_pause(control, gh) == retained
    assert control.paused(gh, gh.pr)
    assert gh.pr["labels"] == [{"name": control.PAUSED}]
    assert not control.approved(gh.repository, gh.pr["number"], gh.pr["head"]["sha"])

    if replacement in {"evidence", "cancel", "veto"}:
        # Recreate the trusted module: durable authority survives another recovery.
        monkeypatch.delitem(controller.MODULES, "review_control")
        restarted = controller.trusted_module("review_control")
        restarted.recover_feature_publication(gh, gh.pr, task_digest="a" * 64)
        assert saved_pause(restarted, gh) == retained
        assert restarted.paused(gh, gh.pr)


@pytest.mark.parametrize("reason", [BINDING, ENROLLMENT, CORRECTION])
def test_recovery_clears_only_its_unchanged_derived_hold(control: Any, reason: str) -> None:
    gh = setup_feature(control, reason)
    for _ in range(2):
        if reason == CORRECTION:
            control.resume_correction(gh, gh.pr)
        else:
            control.recover_feature_publication(gh, gh.pr, task_digest="a" * 64)
        assert saved_pause(control, gh) is None
        assert not control.paused(gh, gh.pr)
    # Label reconciliation and exact-head approval are separate authority paths.
    assert gh.pr["labels"] == [{"name": control.PAUSED}]
    assert not control.approved(gh.repository, gh.pr["number"], gh.pr["head"]["sha"])


@pytest.mark.parametrize("reason", [BINDING, ENROLLMENT, CORRECTION])
def test_recovery_does_not_resume_a_closed_feature(control: Any, reason: str) -> None:
    gh = setup_feature(control, reason)
    original = saved_pause(control, gh)
    gh.pr["state"] = "closed"
    if reason == CORRECTION:
        control.resume_correction(gh, gh.pr)
    else:
        control.recover_feature_publication(gh, gh.pr, task_digest="a" * 64)
    assert saved_pause(control, gh) == original


@pytest.mark.parametrize("replacement", ["evidence", "cancel", "head", "timestamp", "veto"])
def test_correction_resume_preserves_a_concurrent_hold_or_veto(
    control: Any, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    gh = setup_feature(control, CORRECTION)
    original = saved_pause(control, gh)
    assert original is not None
    retained = dict(original)
    if replacement == "evidence":
        retained["reason"] = "Original image evidence is unavailable."
    elif replacement == "cancel":
        retained["reason"] = "Owner cancelled the correction."
    elif replacement == "head":
        retained["head"] = "b" * 40
    if replacement != "veto":
        retained["created"] = 2.0
    connect = control.connect
    injected = []

    @contextmanager
    def interleaved_database() -> Iterator[Any]:
        with connect() as db:

            def execute(sql: str, parameters: Any = ()) -> Any:
                cursor = db.execute(sql, parameters)
                if not injected and sql.startswith("SELECT") and "FROM pauses" in sql:
                    # Materialize the one-row query, then another connection changes
                    # authority before the caller can use the observed pause.
                    rows = cursor.fetchall()
                    with connect() as writer:
                        if replacement == "veto":
                            writer.execute(
                                "INSERT INTO vetoes VALUES (?,?,?,?)",
                                (gh.repository, gh.pr["number"], gh.pr["head"]["sha"], 2.0),
                            )
                        else:
                            writer.execute(
                                "INSERT OR REPLACE INTO pauses VALUES (?,?,?,?,?)",
                                tuple(
                                    retained[key]
                                    for key in ("repository", "number", "head", "reason", "created")
                                ),
                            )
                    injected.append(True)
                    return SimpleNamespace(fetchone=lambda: rows[0] if rows else None)
                return cursor

            yield SimpleNamespace(execute=execute)

    with monkeypatch.context() as race:
        race.setattr(control, "connect", interleaved_database)
        control.resume_correction(gh, gh.pr)
    assert injected == [True]
    assert saved_pause(control, gh) == retained
    assert control.paused(gh, gh.pr)
    assert gh.pr["labels"] == [{"name": control.PAUSED}]
    assert not control.approved(gh.repository, gh.pr["number"], gh.pr["head"]["sha"])
    if replacement in {"evidence", "cancel", "veto"}:
        monkeypatch.delitem(controller.MODULES, "review_control")
        restarted = controller.trusted_module("review_control")
        restarted.resume_correction(gh, gh.pr)
        assert saved_pause(restarted, gh) == retained
        assert restarted.paused(gh, gh.pr)
