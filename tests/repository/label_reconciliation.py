from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from tests.repository._support.label_api import GitHub as GitHub  # noqa: PLC0414
from tests.repository.github_agent import checks, controller
from tests.repository.review_control import control as control  # noqa: PLC0414 - pytest fixture

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def merge_environment(monkeypatch: Any, control: Any) -> Any:
    monkeypatch.setattr(controller, "signed_head", lambda *args: True)
    monkeypatch.setattr(controller, "task_metadata", lambda *args: {"task_digest": "task"})
    monkeypatch.setattr(
        controller, "monitor", lambda gh: (1, {"candidates": {"1:" + "a" * 40: {}}})
    )
    monkeypatch.setattr(controller, "save_monitor", lambda gh, state: None)
    monkeypatch.setattr(controller, "dispatch", lambda *args: None)
    original = controller.trusted_module
    monkeypatch.setattr(
        controller,
        "trusted_module",
        lambda name: (
            SimpleNamespace(cleanup=lambda *args, **kwargs: None)
            if name == "branch_cleanup"
            else original(name)
        ),
    )
    return control


def insert_hold(control: Any, reason: str = "Correction agent requires owner input.") -> None:
    with control.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO pauses VALUES (?,?,?,?,?)",
            ("owner/repo", 1, "a" * 40, reason, 1),
        )


def telegram_approval(control: Any, gh: GitHub) -> dict[str, Any]:
    proto = controller.trusted_module("task_context")
    task = proto.repair_context("task", "author:test")
    control.assess(
        gh.repository, 1, gh.pr["head"]["sha"], proto.digest(task), "Repair controller labels."
    )
    control.approval_notice(gh, gh.pull(1), [".github/agent/controller.py"], task)
    event = control.pending()[0]
    control.delivery(event["key"], message_id=101)
    return {
        "id": "callback",
        "from": {"id": 22},
        "data": "approve:" + event["token"],
        "message": {"message_id": 101, "chat": {"id": -1001}},
    }


@pytest.mark.parametrize("branch", ["codex/orchestrator-task", "codex/telegram-feature-task"])
def test_context_mismatch_notice_keeps_hold(control: Any, branch: str) -> None:
    gh = GitHub()
    gh.pr["head"]["ref"] = branch
    reason = "Feature context does not match the host-owned request."
    control.pause(gh, gh.pr, reason, "Publication enrollment mismatch")
    control.pause(gh, gh.pr, reason, "Publication enrollment mismatch")
    assert len(gh.comments) == 1
    body = gh.comments[0]["body"]
    assert reason in body
    assert "Coordinator must restore and verify" in body
    assert "requires user input" not in body
    assert "/agent resume" not in body
    events = control.pending()
    assert len(events) == 1
    event = events[0]
    payload = json.loads(event["payload"])
    assert payload["reason"] == reason
    assert payload["coordinator_blocked"] is True
    assert "Coordinator must restore and verify" in payload["command"]
    assert "/reply_agent" not in payload["command"]
    assert "/agent resume" not in payload["command"]
    assert event["token"] is None
    assert control.paused(gh, gh.pull(1))
    assert not control.approved(gh.repository, 1, gh.pr["head"]["sha"])
    with control.connect() as db:
        held = dict(db.execute("SELECT * FROM pauses").fetchone())
    assert held["reason"] == reason
    assert held["head"] == gh.pr["head"]["sha"]
    assert "feature" in {item["name"] for item in gh.pr["labels"]}


def test_missing_input_notice_still_requests_owner_answer(control: Any) -> None:
    gh = GitHub()
    control.pause(gh, gh.pr, "Supply the original image.", "Correction requires input")
    assert "@owner This PR requires user input." in gh.comments[0]["body"]
    assert "/agent resume " + gh.pr["head"]["sha"] in gh.comments[0]["body"]
    payload = json.loads(control.pending()[0]["payload"])
    assert payload["command"] == "/agent resume " + gh.pr["head"]["sha"]
    assert payload["coordinator_blocked"] is False
    assert control.paused(gh, gh.pull(1))


def test_normal_telegram_approval_clears_only_stale_label(control: Any) -> None:
    gh = GitHub()
    query = telegram_approval(control, gh)
    control.callback(gh, query, 22, -1001)
    assert control.approved(gh.repository, 1, "a" * 40)
    assert gh.pr["labels"] == [{"name": "feature"}]
    # Repeating the callback is idempotent and never removes unrelated labels.
    count = len(gh.mutations)
    control.callback(gh, query, 22, -1001)
    assert len(gh.mutations) == count


def test_github_approval_repairs_label_before_actual_merge(
    merge_environment: Any,
    tmp_path: Path,
) -> None:
    gh = GitHub()
    gh.owner_approval()
    controller.merge(gh, 17, tmp_path)
    assert gh.pr["state"] == "closed"
    assert gh.mutations == [
        ("issues/1/labels", "PUT"),
        ("pulls/1/merge", "PUT"),
    ]
    assert gh.pr["labels"] == [{"name": "feature"}]


def test_failed_callback_label_update_recovers_on_next_merge_pass(
    merge_environment: Any,
    tmp_path: Path,
) -> None:
    gh = GitHub()
    query = telegram_approval(merge_environment, gh)
    gh.fail_delete = True
    with pytest.raises(RuntimeError, match="Transient label"):
        merge_environment.callback(gh, query, 22, -1001)
    assert merge_environment.approved(gh.repository, 1, "a" * 40)
    gh.fail_delete = False
    controller.merge(gh, 17, tmp_path)
    assert gh.pr["state"] == "closed"


@pytest.mark.parametrize(("head", "actor"), [("b" * 40, "owner"), ("a" * 40, "stranger")])
def test_other_heads_or_actors_cannot_clear_the_label(
    merge_environment: Any,
    tmp_path: Path,
    head: str,
    actor: str,
) -> None:
    gh = GitHub()
    gh.owner_approval(head, actor)
    controller.merge(gh, 17, tmp_path)
    assert not gh.mutations
    assert gh.pr["state"] == "open"


@pytest.mark.parametrize(
    "reason",
    [
        "Correction agent requires owner input.",
        "Feature context does not match the host-owned request.",
    ],
)
def test_approved_policy_does_not_clear_a_question_or_provenance_hold(
    merge_environment: Any,
    tmp_path: Path,
    reason: str,
) -> None:
    gh = GitHub()
    insert_hold(merge_environment, reason)
    merge_environment.callback(gh, telegram_approval(merge_environment, gh), 22, -1001)
    controller.merge(gh, 17, tmp_path)
    assert merge_environment.paused(gh, gh.pull(1))
    assert not gh.mutations


@pytest.mark.parametrize("race", ["head", "question", "veto"])
def test_concurrent_new_head_or_hold_restores_label_and_blocks_merge(
    merge_environment: Any,
    tmp_path: Path,
    race: str,
) -> None:
    gh = GitHub()
    gh.owner_approval()

    def changed() -> None:
        if race == "head":
            gh.pr["head"]["sha"] = "b" * 40
        elif race == "question":
            insert_hold(merge_environment)
        else:
            with merge_environment.connect() as db:
                db.execute("INSERT INTO vetoes VALUES (?,?,?,?)", (gh.repository, 1, "a" * 40, 2))

    gh.on_delete = changed
    controller.merge(gh, 17, tmp_path)
    expected = {"feature"} if race == "head" else {"agent:needs-user", "feature"}
    assert {item["name"] for item in gh.pr["labels"]} == expected
    assert not any(path.endswith("/merge") for path, method in gh.mutations)
    assert gh.pr["state"] == "open"


@pytest.mark.parametrize("gate", ["ci", "review", "writer"])
def test_label_repair_keeps_other_merge_gates(
    merge_environment: Any,
    tmp_path: Path,
    gate: str,
) -> None:
    gh = GitHub()
    gh.owner_approval()
    if gate == "ci":
        gh.fast[1]["conclusion"] = "failure"
    elif gate == "review":
        gh.fast[0]["external_id"] = "old-base"
    else:
        gh.pr["labels"].append({"name": "agent:writing"})
    controller.merge(gh, 17, tmp_path)
    assert "agent:needs-user" not in {item["name"] for item in gh.pr["labels"]}
    assert not any(path.endswith("/merge") for path, method in gh.mutations)
    assert gh.pr["state"] == "open"


@pytest.mark.parametrize("gate", ["ci", "review", "writer", "mergeability"])
def test_unready_policy_candidate_is_not_mislabelled_as_waiting_for_user(
    merge_environment: Any,
    tmp_path: Path,
    gate: str,
) -> None:
    gh = GitHub()
    if gate == "ci":
        gh.fast[1]["conclusion"] = "failure"
    elif gate == "review":
        gh.fast[0]["external_id"] = "old-base"
    elif gate == "writer":
        gh.pr["labels"].append({"name": "agent:writing"})
    else:
        gh.pr["mergeable"] = False
    controller.merge(gh, 17, tmp_path)
    assert "agent:needs-user" not in {item["name"] for item in gh.pr["labels"]}
    assert gh.pr["state"] == "open"
    # Making the same unsigned-policy candidate otherwise ready requests the
    # real owner's decision; removing a misleading label never grants approval.
    gh.fast = checks()
    gh.pr.update(mergeable=True, labels=[{"name": "feature"}])
    controller.merge(gh, 17, tmp_path)
    assert "agent:needs-user" in {item["name"] for item in gh.pr["labels"]}
    assert gh.pr["state"] == "open"


def test_scoped_merge_ignores_continuous_unrelated_work_and_protects_cleanup(
    merge_environment: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    gh = GitHub()
    gh.owner_approval()
    pull = gh.pull
    pulled: list[int] = []
    cleanup_calls: list[dict[str, Any]] = []
    dispatches: list[tuple[Any, ...]] = []
    monkeypatch.setattr(gh, "pulls", lambda: [{"number": 25}, {"number": 1}])

    def selected_pull(number: int) -> dict[str, Any]:
        assert number == 1  # The continuously owned PR is never inspected or mutated.
        pulled.append(number)
        return pull(number)

    def cleanup(actual_gh: Any, work: Path, **kwargs: Any) -> None:
        assert actual_gh is gh
        assert work == tmp_path
        cleanup_calls.append(kwargs)

    original = controller.trusted_module
    monkeypatch.setattr(gh, "pull", selected_pull)
    monkeypatch.setattr(
        controller,
        "trusted_module",
        lambda name: (
            SimpleNamespace(cleanup=cleanup) if name == "branch_cleanup" else original(name)
        ),
    )
    monkeypatch.setattr(controller, "monitor", lambda gh: (1, {"candidates": {}}))
    monkeypatch.setattr(controller, "dispatch", lambda *args: dispatches.append(args))
    active = {"codex/correction-unpublished", "codex/live-writer"}
    # Match the worker's scoped call while unrelated admissions/retries continue.
    controller.merge_selected(gh, 17, tmp_path, pr_numbers={1}, active_branches=active)
    assert gh.pr["state"] == "closed"
    assert set(pulled) == {1}
    assert cleanup_calls == [{"active_branches": active, "force": True}]
    assert active == {"codex/correction-unpublished", "codex/live-writer"}
    assert dispatches == [(gh, "integration", {"candidate": "a" * 40, "number": 1})]
    assert [
        path for path, method in gh.mutations if method == "PUT" and path.endswith("/merge")
    ] == ["pulls/1/merge"]


def test_empty_scoped_selection_never_means_merge_all(
    merge_environment: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    gh = GitHub()
    gh.owner_approval()

    def unexpected_pull(number: int) -> dict[str, Any]:
        pytest.fail("An empty selection must not inspect or mutate a PR")

    monkeypatch.setattr(gh, "pull", unexpected_pull)
    monkeypatch.setattr(gh, "pulls", lambda: [{"number": 1}])
    controller.merge_selected(gh, 17, tmp_path, pr_numbers=set())
    assert not gh.mutations


@pytest.mark.parametrize(
    "gate",
    [
        "ci",
        "review",
        "writer",
        "mergeability",
        "policy",
        "question",
        "signature",
        "base",
    ],
)
def test_scoped_selection_cannot_bypass_merge_authority(
    merge_environment: Any, monkeypatch: Any, tmp_path: Path, gate: str
) -> None:
    gh = GitHub()
    if gate != "policy":
        gh.owner_approval()
    if gate == "ci":
        gh.fast[1]["conclusion"] = "failure"
    elif gate == "review":
        gh.fast[0]["external_id"] = "old-base"
    elif gate == "writer":
        gh.pr["labels"].append({"name": "agent:writing"})
    elif gate == "mergeability":
        gh.pr["mergeable"] = False
    elif gate == "question":
        insert_hold(merge_environment)
    elif gate == "signature":
        monkeypatch.setattr(controller, "signed_head", lambda *args: False)
    elif gate == "base":
        bases = iter(["base", "new-base"])
        monkeypatch.setattr(gh, "base", lambda: next(bases))
    controller.merge_selected(gh, 17, tmp_path, pr_numbers={1})
    assert gh.pr["state"] == "open"
    assert not any(path.endswith("/merge") for path, method in gh.mutations)


def test_scoped_merge_rejects_invalid_task_provenance(
    merge_environment: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    gh = GitHub()
    gh.owner_approval()

    def invalid_metadata(*args: Any) -> Any:
        message = "Signed task context changed"
        raise ValueError(message)

    monkeypatch.setattr(controller, "task_metadata", invalid_metadata)
    with pytest.raises(ValueError, match="Signed task context changed"):
        controller.merge_selected(gh, 17, tmp_path, pr_numbers={1})
    assert gh.pr["state"] == "open"
    assert not any(path.endswith("/merge") for path, method in gh.mutations)


def test_rejected_bot_repair_is_paused_until_owner_resumes(
    monkeypatch: Any, tmp_path: Path
) -> None:
    base = "b" * 40
    pr = {
        "number": 8,
        "draft": False,
        "state": "open",
        "mergeable": True,
        "labels": [],
        "base": {"ref": "dev"},
        "user": {"type": "Bot", "login": "analytics-toolkit-agent-717959[bot]"},
        "head": {
            "sha": "a" * 40,
            "ref": controller.REPAIR_BRANCH + base[:12],
            "repo": {"full_name": "owner/repo"},
        },
    }

    class GitHub:
        repository = "owner/repo"

        def base(self) -> str:
            return base

        def pulls(self) -> list[dict[str, Any]]:
            return [pr]

        def pull(self, number: int) -> dict[str, Any]:
            return pr

        def pages(self, path: str, key: str) -> list[Any]:
            assert "%2B00%3A00" in path
            return []

        def api(self, path: str, method: str = "GET", data: Any = None) -> Any:
            path = path.split("?", 1)[0]
            if path.endswith("/labels"):
                if method in {"POST", "PUT"}:
                    pr["labels"] = [{"name": name} for name in data["labels"]]
                return pr["labels"]
            return {"behind_by": 0}

        def checks(self, head: str) -> list[dict[str, Any]]:
            result = checks(base)
            result[0].update(conclusion="failure", status="completed")
            return result

    monkeypatch.setattr(
        controller, "monitor", lambda gh: (99, {"started": "2026-10-01T00:00:00+00:00", "runs": {}})
    )
    monkeypatch.setattr(controller, "save_monitor", lambda *args: None)
    monkeypatch.setattr(controller, "authorization_error", lambda *args: None)
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(controller, "signed_head", lambda *args: True)
    controls = controller.trusted_module("review_control")
    with controls.connect() as db:
        db.execute(
            "INSERT INTO pauses VALUES (?,?,?,?,?)",
            ("owner/repo", 8, "a" * 40, "Scope mismatch", 1),
        )
    gh = GitHub()
    original_pages = gh.pages
    gh.pages = lambda path, key=None: [] if "/comments?" in path else original_pages(path, key)
    assert controller.discover(gh, 17)["include"] == []
    assert pr["labels"] == [{"name": "agent:needs-user"}]


def test_revision_retry_backlog_is_suppressed_without_losing_originals(control: Any) -> None:
    payload = {
        "text": "Worker deferred this phase; private logs retained. Retry is queued.",
        "request_id": "69db3ccc1579",
        "telegram_feature": False,
        "turn_revision": 30,
        "render_version": 1,
        "rendered_parts": ["previously rendered status"],
        "next_part": 1,
    }
    # Reproduce the observed distinct keys, including pre-existing queued rows
    # that bypass any future enqueue-time filter.
    with control.connect() as db:
        for revision in range(30, 116):
            db.execute(
                "INSERT INTO notifications(key,kind,payload,message_id) VALUES (?,?,?,?)",
                (
                    f"request:69db3ccc1579:{revision}:plan:deferred",
                    "request",
                    json.dumps(payload),
                    724,
                ),
            )
    control.enqueue("approval:current", "approval", {"head": "a" * 40}, buttons=True)
    events = control.pending()
    assert [event["key"] for event in events] == ["approval:current"]
    assert events[0]["token"]
    with control.connect() as db:
        rows = db.execute("SELECT * FROM notifications WHERE kind='request'").fetchall()
    assert len(rows) == 86
    assert all(row["sent"] == -1 and row["message_id"] == 724 for row in rows)
    assert all(json.loads(row["payload"]) == payload for row in rows)
    control.delivery("approval:current", delay=30)
    assert control.pending() == []


@pytest.mark.parametrize(
    ("key", "kind", "payload"),
    [
        ("request:local:31:waiting", "request", {"telegram_feature": False}),
        ("request:telegram:31:plan:deferred", "request", {"telegram_feature": True}),
        ("legacy-request", "request", {"text": "Origin not established"}),
        ("confirm:answer", "request-auth", {"telegram_feature": False}),
        ("approval:head", "approval", {}),
        ("merged:dev", "merged", {}),
        ("main:promotion", "main", {}),
        ("release:version", "release", {}),
    ],
)
def test_filter_preserves_questions_and_existing_delivery_retry_contract(
    control: Any, key: str, kind: str, payload: dict[str, Any]
) -> None:
    control.enqueue(key, kind, payload)
    event = control.pending()[0]
    assert event["key"] == key
    assert json.loads(event["payload"]) == payload
    control.delivery(key, delay=30)
    assert control.pending() == []
    control.delivery(key, message_id=99)
    assert control.pending() == []


def test_scan_and_delivery_batches_stay_bounded(control: Any) -> None:
    for revision in range(205):
        control.enqueue(
            f"request:local:{revision}:plan:deferred",
            "request",
            {"telegram_feature": False},
        )
    for number in range(25):
        control.enqueue(f"approval:{number}", "approval", {})
    assert control.pending() == []
    events = control.pending()
    assert len(events) == 20
    for event in events:
        control.delivery(event["key"], message_id=1)
    assert len(control.pending()) == 5


def test_historically_suppressed_owner_notice_is_left_untouched(control: Any) -> None:
    with control.connect() as db:
        db.execute(
            "INSERT INTO notifications(key,kind,payload,sent,retry,token,message_id) "
            "VALUES (?,?,?,-1,?,?,?)",
            (
                "request:local:30:context-blocked",
                "request",
                json.dumps({"telegram_feature": False, "text": "Send a correction."}),
                123,
                "retained-receipt",
                724,
            ),
        )
        original = dict(db.execute("SELECT * FROM notifications").fetchone())
    assert control.pending() == []
    with control.connect() as db:
        assert dict(db.execute("SELECT * FROM notifications").fetchone()) == original


@pytest.mark.parametrize("pre_existing", [False, True])
def test_context_blocked_correction_notice_survives_status_suppression(
    control: Any, monkeypatch: Any, pre_existing: bool
) -> None:
    monkeypatch.setattr(control.time, "time", lambda: 1000)
    revision = 30
    key = f"request:69db3ccc1579:{revision}:context-blocked"
    # Intake completes the waiting turn before building this notice, incrementing
    # turn_revision. No waiting_notice means no separate :waiting event is queued.
    payload = {
        "title": "Feature request 69db3ccc1579",
        "text": "Task context exceeds the limit. Request paused; send a correction.",
        "request_id": "69db3ccc1579",
        "turn_revision": revision + 1,
        "telegram_feature": False,
    }
    original = json.dumps(payload)
    status_key = f"request:69db3ccc1579:{revision}:plan:deferred"
    control.enqueue(status_key, "request", {"telegram_feature": False})
    if pre_existing:
        with control.connect() as db:
            db.execute(
                "INSERT INTO notifications(key,kind,payload,message_id) VALUES (?,?,?,?)",
                (key, "request", original, 724),
            )
    else:
        control.enqueue(key, "request", payload)
    events = control.pending()
    assert [event["key"] for event in events] == [key]
    event = events[0]
    assert event["payload"] == original
    assert event["message_id"] == (724 if pre_existing else None)
    with control.connect() as db:
        status = db.execute("SELECT * FROM notifications WHERE key=?", (status_key,)).fetchone()
        assert status["sent"] == -1

    control.delivery(key, delay=30)
    assert control.pending() == []
    with control.connect() as db:
        delayed = dict(db.execute("SELECT * FROM notifications WHERE key=?", (key,)).fetchone())
    assert delayed == {**event, "retry": 1030}
    monkeypatch.setattr(control.time, "time", lambda: 1030)
    assert control.pending() == [delayed]
    control.delivery(key, message_id=99)
    assert control.pending() == []
    with control.connect() as db:
        delivered = dict(db.execute("SELECT * FROM notifications WHERE key=?", (key,)).fetchone())
    assert delivered == {**delayed, "sent": 1, "message_id": 99}


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "missing-digest",
        "wrong-digest",
        "wrong-head",
        "owner-hold",
        "cancelled",
        "veto",
        "closed",
        "other-branch",
    ],
)
def test_orchestrator_publication_recovers_only_enrolled_exact_head(
    control: Any,
    case: str,
) -> None:
    gh = GitHub()
    gh.pr["head"]["ref"] = "codex/orchestrator-task"
    gh.pr["labels"] = []
    digest = "a" * 64
    control.enroll_feature(gh.repository, gh.pr["head"]["ref"], digest)
    reason = "Feature context does not match the host-owned request."
    if case == "owner-hold":
        reason = "Owner changed the request; renewed planning is required."
    elif case == "cancelled":
        reason = "Owner cancelled the request."
    control.pause(gh, gh.pr, reason, "Publication enrollment mismatch")
    if case == "wrong-head":
        gh.pr["head"]["sha"] = "b" * 40
    elif case == "veto":
        with control.connect() as db:
            db.execute(
                "INSERT INTO vetoes VALUES (?,?,?,?)",
                (gh.repository, gh.pr["number"], gh.pr["head"]["sha"], 1.0),
            )
    elif case == "closed":
        gh.pr["state"] = "closed"
    elif case == "other-branch":
        gh.pr["head"]["ref"] = "codex/owner-task"
    if case == "wrong-digest":
        digest = "b" * 64
    control.recover_feature_publication(
        gh,
        gh.pr,
        task_digest=None if case == "missing-digest" else digest,
    )
    with control.connect() as db:
        held = db.execute("SELECT reason FROM pauses").fetchone()
    assert (held is None) == (case == "valid")
    # Renewed planning remains held without manufacturing an owner question.
    expected = [] if case in {"owner-hold", "valid"} else [{"name": control.PAUSED}]
    assert gh.pr["labels"] == expected
    assert not control.approved(gh.repository, gh.pr["number"], gh.pr["head"]["sha"])
