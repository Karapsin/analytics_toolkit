"""Status producers converge immediately without a scheduled worker repair."""

from __future__ import annotations

import ast
import copy
import hashlib
import os
import sqlite3
from contextlib import closing
from importlib import import_module
from typing import TYPE_CHECKING, Any

import pytest

from tests._support.paths import REPO_ROOT
from tests.repository.github_agent import controller
from tests.repository.label_reconciliation import GitHub, telegram_approval
from tests.repository.review_control import control as control  # noqa: PLC0414 - fixture

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType


PHASES = (
    "agent:writing",
    "agent:planning",
    "agent:reviewing",
    "agent:queued",
    "agent:testing",
    "agent:ready",
)


class ProducerGitHub(GitHub):
    def __init__(self, phase: str = "agent:reviewing") -> None:
        super().__init__()
        self.pr["labels"] = [{"name": phase}, {"name": "feature"}]
        self.before: Any = None
        self.after: Any = None

    def api(self, path: str, method: str = "GET", data: Any = None) -> Any:
        if self.before:
            self.before(path, method)
        result = super().api(path, method, data)
        if path.startswith("pulls/") and method == "PATCH":
            self.pr.update(data)
        if self.after:
            self.after(path, method)
        return result


def names(gh: GitHub) -> set[str]:
    return {item["name"] for item in gh.pr["labels"]}


def authority(control: Any) -> dict[str, list[Any]]:
    with control.connect() as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {
            table: [tuple(r) for r in db.execute('SELECT * FROM "' + table + '"')]
            for table in tables
        }


def insert_question(control: Any, gh: GitHub) -> None:
    with control.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO pauses VALUES (?,?,?,?,?)",
            (gh.repository, 1, gh.pr["head"]["sha"], "A genuine unanswered question.", 100),
        )


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize(
    "producer", ["pause", "question", "cancel", "feature-cancel", "approval", "veto"]
)
def test_attention_producers_remove_each_phase_immediately(
    control: Any,
    phase: str,
    producer: str,
) -> None:
    gh = ProducerGitHub(phase)
    if producer == "pause":
        control.pause(gh, gh.pr, "Owner must provide missing evidence.", "Preserve task context.")
    elif producer in {"question", "cancel"}:
        control.hold_correction(gh, gh.pr, cancel=producer == "cancel")
    elif producer == "feature-cancel":
        control.enroll_feature(gh.repository, gh.pr["head"]["ref"], "d" * 64)
        control.hold_feature(gh, gh.pr, cancel=True)
        assert gh.pr["state"] == "closed"
    elif producer == "veto":
        query = telegram_approval(control, gh)
        query["data"] = query["data"].replace("approve:", "disapprove:")
        control.callback(gh, query, 22, -1001)
    else:
        assert control.reconcile_label(gh, gh.pr, approval_required=True)
    assert names(gh) == {"feature", control.PAUSED}
    assert not names(gh) & set(PHASES)


@pytest.mark.parametrize("phase", PHASES)
def test_renewed_planning_is_held_without_owner_attention(control: Any, phase: str) -> None:
    gh = ProducerGitHub(phase)
    gh.pr["labels"].append({"name": control.PAUSED})
    control.enroll_feature(gh.repository, gh.pr["head"]["ref"], "d" * 64)
    control.hold_feature(gh, gh.pr)
    assert names(gh) == {"feature", phase}
    assert control.paused(gh, gh.pr)
    assert not control.owner_attention(gh, gh.pr)


@pytest.mark.parametrize("existing", ["display", "question", "veto"])
def test_controller_writer_cannot_override_attention(control: Any, existing: str) -> None:
    gh = ProducerGitHub()
    if existing == "display":
        gh.pr["labels"].append({"name": control.PAUSED})
    elif existing == "question":
        insert_question(control, gh)
    else:
        with control.connect() as db:
            db.execute("INSERT INTO vetoes VALUES (?,?,?,?)", (gh.repository, 1, "a" * 40, 100))
    before = authority(control)
    controller.GitHub.label(gh, 1, acquire=True)
    assert names(gh) == {"feature", control.PAUSED}
    assert authority(control) == before


def test_writer_release_preserves_a_new_review_phase(control: Any) -> None:
    gh = ProducerGitHub("agent:writing")
    gh.pr["labels"].append({"name": "agent:reviewing"})
    before = authority(control)
    controller.GitHub.label(gh, 1, acquire=False)
    assert names(gh) == {"feature", "agent:reviewing"}
    assert authority(control) == before


@pytest.mark.parametrize("replacement", ["none", "question", "veto"])
def test_derived_recovery_refreshes_display_and_preserves_new_authority(
    control: Any,
    replacement: str,
) -> None:
    gh = ProducerGitHub()
    control.hold_correction(gh, gh.pr)
    observed = control.attention_snapshot(gh, gh.pr)
    if replacement == "question":
        insert_question(control, gh)
    elif replacement == "veto":
        with control.connect() as db:
            db.execute("INSERT INTO vetoes VALUES (?,?,?,?)", (gh.repository, 1, "a" * 40, 100))
    before = authority(control)
    control._retire_derived_pause(gh, gh.pr, observed)
    assert names(gh) == ({"feature"} if replacement == "none" else {"feature", control.PAUSED})
    after = authority(control)
    if replacement == "none":
        assert not after.pop("pauses")
        before.pop("pauses")
    assert after == before


def test_resume_retries_failed_display_after_successful_exact_retirement(control: Any) -> None:
    gh = ProducerGitHub()
    control.hold_correction(gh, gh.pr)
    gh.fail_delete = True
    with pytest.raises(RuntimeError, match="Transient label"):
        control.resume_correction(gh, gh.pr)
    assert control.attention_snapshot(gh, gh.pr) is None
    assert control.PAUSED in names(gh)
    gh.fail_delete = False
    control.resume_correction(gh, gh.pr)
    assert names(gh) == {"feature"}


@pytest.mark.parametrize(
    "kind",
    [
        "absent",
        "correction",
        "stale-correction",
        "dependency",
        "renewal",
        "stale-renewal",
        "amendment",
    ],
)
def test_internal_work_is_not_protected_approval_readiness(
    control: Any,
    monkeypatch: Any,
    kind: str,
) -> None:
    gh = ProducerGitHub()
    monkeypatch.setattr(
        controller, "authorization_error", lambda *args, **kwargs: "Policy approval needed"
    )
    monkeypatch.setattr(controller, "approval_ready", lambda *args: True)
    with control.connect() as db:
        if "correction" in kind:
            db.execute(
                "INSERT INTO corrections VALUES (?,?,?,?,?,?,?)",
                (
                    gh.repository,
                    1,
                    "b" * 40 if kind.startswith("stale") else "a" * 40,
                    "digest",
                    "Fresh planning",
                    "Preserve task",
                    1,
                ),
            )
        elif kind == "dependency":
            db.execute(
                "CREATE TABLE coordinator_holds "
                "(repository TEXT,number INTEGER,request TEXT,binding TEXT)"
            )
            db.execute(
                "INSERT INTO coordinator_holds VALUES (?,?,?,?)",
                (gh.repository, 1, "request", "{}"),
            )
        elif "renewal" in kind:
            db.execute(
                "CREATE TABLE coordinator_renewals "
                "(repository TEXT,number INTEGER,request TEXT,binding TEXT,head TEXT,digest TEXT)"
            )
            db.execute(
                "INSERT INTO coordinator_renewals VALUES (?,?,?,?,?,?)",
                (
                    gh.repository,
                    1,
                    "request",
                    "{}",
                    "b" * 40 if kind.startswith("stale") else "a" * 40,
                    "digest",
                ),
            )
        elif kind == "amendment":
            db.execute(
                "CREATE TABLE feature_amendments "
                "(repository TEXT,number INTEGER,head TEXT,created REAL)"
            )
            db.execute(
                "INSERT INTO feature_amendments VALUES (?,?,?,?)", (gh.repository, 1, "a" * 40, 1)
            )
    before = authority(control)
    assert controller.approval_attention(gh, gh.pr, 17) == (
        kind in {"absent", "stale-correction", "stale-renewal"}
    )
    assert authority(control) == before


def test_present_malformed_internal_gate_is_an_error_not_ready(control: Any) -> None:
    gh = ProducerGitHub()
    with control.connect() as db:
        db.execute("CREATE TABLE coordinator_holds (malformed TEXT)")
    before = authority(control)
    with pytest.raises(sqlite3.OperationalError):
        control.display_work_pending(gh, gh.pr)
    assert authority(control) == before


def test_internal_work_projection_uses_one_snapshot_during_amendment_transfer(
    control: Any,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    gh = ProducerGitHub()
    original_connect = control.connect
    with original_connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE feature_amendments "
            "(repository TEXT,number INTEGER,head TEXT,created REAL)"
        )
        db.execute(
            "INSERT INTO feature_amendments VALUES (?,?,?,?)", (gh.repository, 1, "a" * 40, 1)
        )
    transferred = False

    class ObservedConnection:
        def __init__(self) -> None:
            self.db = original_connect()

        def __enter__(self) -> Any:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool:
            try:
                return bool(self.db.__exit__(exc_type, exc_value, traceback))
            finally:
                self.db.close()

        def execute(self, query: str, parameters: Any = ()) -> Any:
            nonlocal transferred
            cursor = self.db.execute(query, parameters)
            if query.startswith("SELECT 1 FROM pauses") and not transferred:
                with closing(
                    sqlite3.connect(tmp_path / "review_control.sqlite3")
                ) as writer, writer:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute(
                        "INSERT INTO pauses VALUES (?,?,?,?,?)",
                        (
                            gh.repository,
                            1,
                            "a" * 40,
                            "Owner changed the request; renewed planning is required.",
                            2,
                        ),
                    )
                    writer.execute("DELETE FROM feature_amendments")
                transferred = True
            return cursor

    monkeypatch.setattr(control, "connect", ObservedConnection)
    assert control.display_work_pending(gh, gh.pr)
    assert transferred
    with original_connect() as db:
        assert db.execute("SELECT count(*) FROM pauses").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM feature_amendments").fetchone()[0] == 0


@pytest.mark.parametrize("method", ["DELETE", "POST"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_attention_arriving_during_each_phase_mutation_wins(
    control: Any,
    method: str,
    timing: str,
) -> None:
    gh = ProducerGitHub("agent:planning")
    fired = False

    def incoming(path: str, operation: str) -> None:
        nonlocal fired
        if operation == method and not fired:
            fired = True
            insert_question(control, gh)

    setattr(gh, timing, incoming)
    before = authority(control)
    result = control.status_transition(gh, 1, "a" * 40, lambda fresh: "agent:writing")
    assert result is not None
    assert fired
    assert names(gh) == {"feature", control.PAUSED}
    after = authority(control)
    assert after.pop("pauses")
    before.pop("pauses")
    assert after == before


def test_reentrant_owner_producer_converges_without_file_lock_deadlock(control: Any) -> None:
    gh = ProducerGitHub("agent:planning")
    fired = False

    def incoming(path: str, operation: str) -> None:
        nonlocal fired
        if operation == "POST" and not fired:
            fired = True
            control.hold_correction(gh, gh.pr)

    gh.after = incoming
    controller.GitHub.label(gh, 1, acquire=True)
    assert fired
    assert names(gh) == {"feature", control.PAUSED}


@pytest.mark.parametrize("change", ["head", "closed"])
def test_changed_head_or_state_aborts_without_invented_attention(control: Any, change: str) -> None:
    gh = ProducerGitHub()
    gh.pr["labels"].append({"name": control.PAUSED})

    def changed(path: str, operation: str) -> None:
        if operation == "DELETE":
            if change == "head":
                gh.pr["head"]["sha"] = "b" * 40
            else:
                gh.pr["state"] = "closed"

    gh.after = changed
    assert not control.reconcile_label(gh, copy.deepcopy(gh.pr), approval_required=False)
    assert control.PAUSED not in names(gh)
    assert not any(operation == "POST" for path, operation in gh.mutations)


def test_issue_label_endpoint_refreshes_the_callers_metadata(control: Any) -> None:
    gh = ProducerGitHub("agent:testing")
    stale = copy.deepcopy(gh.pr)
    stale["labels"] = [{"name": control.PAUSED}]
    assert control.reconcile_label(gh, stale, approval_required=False)
    assert {item["name"] for item in stale["labels"]} == {"feature", "agent:testing"}


@pytest.mark.parametrize("already_absent", [True, False])
def test_delete_errors_require_fresh_authenticated_absence(
    control: Any, already_absent: bool
) -> None:
    gh = ProducerGitHub()
    original = gh.api

    def api(path: str, method: str = "GET", data: Any = None) -> Any:
        if method == "DELETE":
            if already_absent:
                original(path, method, data)
            message = "Persistent permission error"
            raise RuntimeError(message)
        return original(path, method, data)

    gh.api = api
    if already_absent:
        controller.GitHub.label(gh, 1, acquire=True)
        assert names(gh) == {"feature", "agent:writing"}
    else:
        with pytest.raises(RuntimeError, match="permission"):
            controller.GitHub.label(gh, 1, acquire=True)
        assert names(gh) == {"feature", "agent:reviewing"}


def test_failed_post_retries_without_changing_authority(control: Any) -> None:
    gh = ProducerGitHub()
    original = gh.api

    def api(path: str, method: str = "GET", data: Any = None) -> Any:
        if method == "POST":
            message = "Network unavailable"
            raise RuntimeError(message)
        return original(path, method, data)

    gh.api = api
    before = authority(control)
    with pytest.raises(RuntimeError, match="Network"):
        controller.GitHub.label(gh, 1, acquire=True)
    assert authority(control) == before
    assert names(gh) == {"feature"}
    gh.api = original
    controller.GitHub.label(gh, 1, acquire=True)
    assert names(gh) == {"feature", "agent:writing"}
    assert authority(control) == before


def test_pure_approval_projection_cannot_recover_owner_authority(
    control: Any,
    monkeypatch: Any,
) -> None:
    gh = ProducerGitHub()
    gh.pr["labels"] = [{"name": "feature"}]
    gh.pr["mergeable"] = True
    gh.owner_approval()
    monkeypatch.setattr(controller, "signed_head", lambda *args: True)
    before = authority(control)
    assert controller.authorization_error(gh, gh.pr, recover=False) is None
    assert not controller.approval_attention(gh, gh.pr, 17)
    assert authority(control) == before
    insert_question(control, gh)
    before = authority(control)
    assert controller.authorization_error(gh, gh.pr, recover=False)
    assert not controller.approval_attention(gh, gh.pr, 17)
    assert authority(control) == before


def test_closed_cleanup_preserves_live_future_and_attention(control: Any) -> None:
    gh = ProducerGitHub("agent:writing")
    gh.pr["state"] = "closed"
    before = authority(control)
    assert control.status_transition(gh, 1, "a" * 40, lambda fresh: None, expected_state="closed")
    assert names(gh) == {"feature", "agent:writing"}
    assert control.status_transition(gh, 1, "a" * 40, lambda fresh: "", expected_state="closed")
    assert names(gh) == {"feature"}
    assert authority(control) == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX host advisory lock")
def test_busy_pr_lock_is_bounded_and_retryable(control: Any, tmp_path: Path) -> None:
    fcntl = import_module("fcntl")

    gh = ProducerGitHub()
    root = tmp_path / "status-locks"
    root.mkdir()
    name = hashlib.sha256(f"{gh.repository}:1".encode()).hexdigest() + ".lock"
    with (root / name).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            controller.GitHub.label(gh, 1, acquire=True)
        assert not gh.mutations
    controller.GitHub.label(gh, 1, acquire=True)
    assert names(gh) == {"feature", "agent:writing"}


def test_controller_status_mutations_have_one_boundary() -> None:
    """AST inventory guards against a new append-only producer elsewhere."""
    mutations = []
    for path in sorted((REPO_ROOT / ".github/agent").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if (
                    not isinstance(call, ast.Call)
                    or not isinstance(call.func, ast.Attribute)
                    or call.func.attr != "api"
                    or len(call.args) < 2
                ):
                    continue
                method = call.args[1]
                route = call.args[0]
                fragments = (
                    [part.value for part in route.values if isinstance(part, ast.Constant)]
                    if isinstance(route, ast.JoinedStr)
                    else [getattr(route, "value", "")]
                )
                if (
                    isinstance(method, ast.Constant)
                    and method.value in {"POST", "PUT", "DELETE"}
                    and any("/labels" in str(part) for part in fragments)
                ):
                    mutations.append((path.name, node.name))
    assert mutations
    assert set(mutations) == {("review_control.py", "status_transition")}
