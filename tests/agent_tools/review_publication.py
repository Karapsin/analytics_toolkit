"""Direct publication pins its reviewed subject and cannot bypass visual checks."""

from __future__ import annotations

import json

import pytest

from agent_tools import local_review, sessions, sql_explorer_visual, task_context
from tests.agent_tools._support.local_review import signed_receipt
from tests.agent_tools.local_review_evidence import candidate as candidate  # noqa: PLC0414
from tests.agent_tools.local_review_evidence import commit, complete_visual_review, visual_candidate
from tests.agent_tools.sessions import source as source  # noqa: PLC0414


def test_direct_push_requires_visuals_before_task_archiving(candidate, monkeypatch):
    visual_candidate(candidate)
    signed_receipt(candidate)
    archived = []
    monkeypatch.setattr(
        task_context, "publish_attachment", lambda *_a, **_kw: archived.append(True)
    )
    with pytest.raises(RuntimeError, match="Completed visual review"):
        sessions.push(candidate)
    assert archived == []


@pytest.mark.parametrize("phase", ["approval", "transport", "archive"])
def test_publication_never_overwrites_the_approved_head_during_a_race(
    candidate, monkeypatch, phase
):
    signed_receipt(candidate)
    real_verified = local_review.verified
    checked = False
    archived = []
    pushed = []
    real_run = sessions.run

    def mutate():
        commit(candidate, {"race": "unreviewed source\n"})

    def verified(root, head):
        nonlocal checked
        receipt = real_verified(root, head)
        if phase == "approval" and not checked:
            checked = True
            mutate()
        return receipt

    def publication(context, **kwargs):
        if phase == "transport":
            mutate()
        return original_publication(context, **kwargs)

    def archive(root, **_kwargs):
        archived.append(True)
        if phase == "archive":
            mutate()
        return task_context.attachment(root)

    def run(root, *args):
        if args[:2] == ("git", "push"):
            pushed.append(args)
            pytest.fail("an unreviewed head was submitted")
        return real_run(root, *args)

    protocol = task_context.protocol()
    original_publication = protocol.publication
    monkeypatch.setattr(local_review, "verified", verified)
    monkeypatch.setattr(protocol, "publication", publication)
    monkeypatch.setattr(task_context, "protocol", lambda: protocol)
    monkeypatch.setattr(task_context, "publish_attachment", archive)
    monkeypatch.setattr(sessions, "run", run)
    with pytest.raises((RuntimeError, ValueError), match=r"changed|exact source|review"):
        sessions.push(candidate)
    assert pushed == []
    assert archived == ([True] if phase == "archive" else [])


def test_publication_rechecks_task_identity_after_transport_validation(candidate, monkeypatch):
    signed_receipt(candidate)
    protocol = task_context.protocol()
    original = protocol.publication
    archived = []

    def publication(context, **kwargs):
        result = original(context, **kwargs)
        path = task_context.directory(candidate) / "active.json"
        value = json.loads(path.read_text())
        value["corrections"].append("New scope after approval")
        path.write_text(json.dumps(value))
        return result

    monkeypatch.setattr(protocol, "publication", publication)
    monkeypatch.setattr(task_context, "protocol", lambda: protocol)
    monkeypatch.setattr(
        task_context, "publish_attachment", lambda *_a, **_kw: archived.append(True)
    )
    with pytest.raises((RuntimeError, ValueError), match=r"changed|digest|context|source|task"):
        sessions.push(candidate)
    assert archived == []


def test_push_visual_selection_uses_the_reviewed_base_not_mutable_origin(candidate, monkeypatch):
    visual_candidate(candidate)
    receipt = signed_receipt(candidate)
    subject = local_review.binding(candidate, receipt["payload"]["head"])
    complete_visual_review(candidate)
    paths = []
    verify = sql_explorer_visual.verify_visual_receipt

    def verify_paths(root, **kwargs):
        paths.extend(kwargs["paths"])
        return verify(root, **kwargs)

    monkeypatch.setattr(sql_explorer_visual, "verify_visual_receipt", verify_paths)
    sessions._publication_ready(candidate, sessions.load(candidate), subject, receipt)
    assert "analytics_toolkit/sql_explorer/app.py" in paths


def test_push_submits_the_explicit_reviewed_object_even_if_head_moves_after_final_check(
    candidate, monkeypatch
):
    receipt = signed_receipt(candidate)
    reviewed = receipt["payload"]["head"]
    state = sessions.load(candidate)
    actual = []
    real_run = sessions.run

    def run(root, *args):
        if args[:2] == ("git", "push"):
            # Model a last-instant branch movement; the remote must still get the
            # reviewed object, never whatever HEAD happens to name now.
            commit(candidate, {"last-instant-change": "unreviewed\n"})
            actual.append(args)
            return ""
        return real_run(root, *args)

    monkeypatch.setattr(sessions, "run", run)
    monkeypatch.setattr(
        task_context, "publish_attachment", lambda root, **_kw: task_context.attachment(root)
    )
    monkeypatch.setattr(sessions, "handoff", lambda _root: {"url": "https://github.test/pull/12"})
    result = sessions.push(candidate)
    assert actual == [("git", "push", "origin", f"{reviewed}:refs/heads/{state['branch']}")]
    assert result["sha"] == reviewed
    assert sessions.load(candidate)["publication_sha"] == reviewed
    assert sessions.run(candidate, "git", "config", f"branch.{state['branch']}.remote") == "origin"
    assert (
        sessions.run(candidate, "git", "config", f"branch.{state['branch']}.merge")
        == f"refs/heads/{state['branch']}"
    )
