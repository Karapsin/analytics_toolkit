from __future__ import annotations

import json
from typing import Any

import pytest

from tests.repository.github_agent import controller


@pytest.fixture
def control(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    return controller.trusted_module("review_control")


def test_retry_outbox_deduplicates_event_and_keeps_actionable_explanation(control: Any) -> None:
    control.enqueue("one", "approval", {"changes": "Expanded scope", "reason": "Not requested"})
    control.enqueue("one", "approval", {"changes": "Duplicate"})
    event = control.pending()[0]
    assert json.loads(event["payload"])["reason"] == "Not requested"
    control.delivery("one", delay=30)
    assert not control.pending()
    control.delivery("one", message_id=10)
    assert not control.pending()


def test_notification_selection_filters_new_and_legacy_noise(control: Any) -> None:
    for kind in ("approval", "request-auth", "merged", "main", "release"):
        control.enqueue(kind, kind, {})
    control.enqueue("telegram", "request", {"telegram_feature": True})
    control.enqueue("status", "request", {"informational": True})
    control.enqueue("local", "request", {"request_id": "local-correction"})
    control.enqueue("rejected", "rejection", {"reason": "Needs corrections"})
    with control.connect() as db:
        db.execute(
            "INSERT INTO notifications(key,kind,payload) VALUES ('old-noise','rejection','{}')"
        )
    events = control.pending()
    assert {event["key"] for event in events} == {
        "approval",
        "request-auth",
        "merged",
        "main",
        "release",
        "telegram",
        "status",
    }
    with control.connect() as db:
        assert (
            db.execute("SELECT sent FROM notifications WHERE key='old-noise'").fetchone()[0] == -1
        )
