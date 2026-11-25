"""Signed journal checkpoints preserve ordered partial evidence and recover writes."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agent_tools import review_journal, review_waves
from tests.agent_tools.local_review_evidence import (
    candidate as candidate,  # noqa: PLC0414 - shared fixture or compatibility export.
)
from tests.agent_tools.review_waves import (
    assigned_candidate as assigned_candidate,  # noqa: PLC0414 - shared fixture or compatibility export.
)
from tests.agent_tools.review_waves import observed_wave
from tests.agent_tools.sessions import (
    source as source,  # noqa: PLC0414 - shared fixture or compatibility export.
)


def later_state(root, plan):
    state = review_waves._read(root)
    wave = observed_wave(
        root, plan, {plan["chunks"][0]["id"]: {"approved": False, "coverage": False}}
    )
    state["waves"].append(wave)
    return state


def test_checkpoint_signature_protects_independent_partial_journal(assigned_candidate):
    root, plan = assigned_candidate
    state = later_state(root, plan)
    review_waves._save(root, state)
    target = review_waves._path(root)
    log = review_journal._log_path(target)
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records[-1]["payload"]["state_sha256"] == review_journal._digest(
        review_journal._data(state)
    )
    records[-1]["payload"]["sequence"] += 1
    log.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    with pytest.raises(ValueError, match="signature"):
        review_waves._read(root)


def test_coordinator_save_rejects_rewriting_authenticated_context(assigned_candidate):
    root, plan = assigned_candidate
    state = later_state(root, plan)
    review_waves._save(root, state)
    changed = copy.deepcopy(state)
    changed["waves"][-1]["iteration"]["instructions"] = "Altered immutable wave context"
    with pytest.raises(ValueError, match="rewrite signed partial outcome history"):
        review_waves._save(root, changed)
    assert review_waves._read(root)["waves"] == state["waves"]


def test_signed_pending_state_recovers_only_its_authorized_atomic_rename(
    assigned_candidate, monkeypatch
):
    root, plan = assigned_candidate
    state = later_state(root, plan)
    target = review_waves._path(root)
    pending = target.with_suffix(".pending.json")
    replace = Path.replace
    message = "Simulated interruption after durable checkpoint append"

    def interrupted(source, destination):
        if source == pending and destination == target:
            raise OSError(message)
        return replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", interrupted)
        with pytest.raises(OSError, match="Simulated interruption"):
            review_waves._save(root, state)
    assert pending.is_file()
    recovered = review_waves._read(root)
    assert recovered["waves"] == state["waves"]
    assert recovered[review_journal.AUTHENTICATION]["authenticated"] is True
    assert not pending.exists()


def test_unsealed_legacy_facts_remain_available_but_cannot_supply_final_approval(
    assigned_candidate,
):
    root, _ = assigned_candidate
    target = review_waves._path(root)
    review_journal._log_path(target).unlink()
    legacy = review_waves._read(root)
    assert legacy["waves"]
    assert legacy[review_journal.AUTHENTICATION]["authenticated"] is False
    assert review_journal.approval_state(legacy)["waves"] == []
