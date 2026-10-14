from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from tests.repository.github_agent import checks, controller, owned_pr
from tests.repository.review_control import control as control  # noqa: PLC0414 - pytest fixture

if TYPE_CHECKING:
    from pathlib import Path


class GitHub:
    repository = "owner/repo"

    def __init__(self) -> None:
        self.pr = owned_pr()
        self.pr.update(mergeable=True, labels=[{"name": "agent:needs-user"}, {"name": "feature"}])
        self.comments: list[dict[str, Any]] = []
        self.mutations: list[tuple[str, str]] = []
        self.on_delete: Any = None
        self.fail_delete = False
        self.fast = checks()

    def pull(self, number: int) -> dict[str, Any]:
        return copy.deepcopy(self.pr)

    def pulls(self) -> list[dict[str, Any]]:
        return [self.pull(1)]

    def base(self) -> str:
        return "base"

    def checks(self, head: str) -> list[dict[str, Any]]:
        return self.fast

    def pages(self, path: str) -> list[dict[str, Any]]:
        if "/files?" in path:
            return [{"filename": ".github/agent/controller.py"}]
        return self.comments

    def comment(self, number: int, body: str) -> None:
        self.comments.append({"body": body, "user": {"type": "Bot", "login": "app"}})

    def api(self, path: str, method: str = "GET", data: Any = None) -> Any:
        if method != "GET":
            self.mutations.append((path, method))
        if path.endswith("/labels") and method == "POST":
            names = {item["name"] for item in self.pr["labels"]}
            self.pr["labels"].extend({"name": name} for name in data["labels"] if name not in names)
        elif "/labels/" in path and method == "DELETE":
            if self.fail_delete:
                message = "Transient label API failure"
                raise RuntimeError(message)
            self.pr["labels"] = [
                item for item in self.pr["labels"] if item["name"] != "agent:needs-user"
            ]
            if self.on_delete:
                self.on_delete()
        elif path.endswith("/merge") and method == "PUT":
            assert data["sha"] == self.pr["head"]["sha"]
            self.pr["state"] = "closed"
            return {"merged": True}
        return copy.deepcopy(self.pr["labels"])

    def owner_approval(self, head: str = "a" * 40, actor: str = "owner") -> None:
        self.comments.append(
            {"body": "/agent approve-policy " + head, "user": {"type": "User", "login": actor}}
        )


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
        ("issues/1/labels/agent:needs-user", "DELETE"),
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


def test_approved_policy_does_not_clear_a_genuine_question_hold(
    merge_environment: Any,
    tmp_path: Path,
) -> None:
    gh = GitHub()
    insert_hold(merge_environment)
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
    assert {item["name"] for item in gh.pr["labels"]} == {"agent:needs-user", "feature"}
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
            if path.endswith("/labels"):
                if method == "POST":
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
