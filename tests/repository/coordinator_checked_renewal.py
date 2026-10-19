"""Actual SQLite atomicity, exact replay and concurrent writer regressions."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from functools import partial
from typing import Any

import pytest

from tests.repository._support.coordinator_authority import Case, durable


def boundary(case: Case, monkeypatch: Any, *, execute: Any = None, commit: Any = None) -> None:
    real_connect = sqlite3.connect

    class Connection:
        def __init__(self, db: Any):
            self.db = db

        def __getattr__(self, key: str) -> Any:
            return getattr(self.db, key)

        def execute(self, sql: str, parameters: Any = ()) -> Any:
            cursor = self.db.execute(sql, parameters)
            if execute is not None:
                execute(self.db, sql)
            return cursor

        def commit(self) -> None:
            if commit is not None:
                commit(self.db)
            else:
                self.db.commit()

    def connect(*args: Any, **kwargs: Any) -> Any:
        db = real_connect(*args, **kwargs)
        return Connection(db) if kwargs.get("uri") and "mode=rw" in str(args[0]) else db

    monkeypatch.setattr(sqlite3, "connect", connect)


def test_commit_then_lost_response_replays_without_mutation(
    tmp_path: Any, monkeypatch: Any
) -> None:
    case = Case(tmp_path, monkeypatch)

    def lose(db: Any) -> None:
        db.commit()
        message = "response lost after actual commit"
        raise sqlite3.OperationalError(message)

    with monkeypatch.context() as patch:
        boundary(case, patch, commit=lose)
        with case.pending("database_unavailable"):
            case.renew()
    state = durable(case.controls)
    result = case.renew()
    assert result["status"] == "replayed"
    assert durable(case.controls) == state
    assert result["renewal_head"] == case.pr["head"]["sha"]
    assert not case.controls.coordinator_held(case.gh, case.pr)
    assert case.controls.correction(case.gh.repository, 1, "1" * 40, "1" * 64)


@pytest.mark.parametrize(
    "field",
    [
        "proof",
        "generation",
        "request_row_sha256",
        "request_revision",
        "request_status",
        "patch_sha256",
        "runtime_sha256",
        "main_head",
    ],
)
def test_replay_cannot_refresh_caller_identity(tmp_path: Any, monkeypatch: Any, field: str) -> None:
    case = Case(tmp_path, monkeypatch)
    assert case.renew()["status"] == "committed"
    before = durable(case.controls)
    if field == "proof":
        case.proof = "e" * 64
        case.caller["proof_sha256"] = case.proof
    elif field == "request_revision":
        case.caller[field] += 1
    elif field == "request_status":
        case.caller[field] = "running"
    else:
        case.caller[field] = "0" * len(case.caller[field])
    with case.pending("receipt_conflict"):
        case.renew()
    assert durable(case.controls) == before


@pytest.mark.parametrize(
    "authority", ["identical", "replacement", "other", "peer", "history", "enrollment"]
)
def test_replay_never_consumes_new_authority(
    tmp_path: Any, monkeypatch: Any, authority: str
) -> None:
    case = Case(tmp_path, monkeypatch)
    case.renew()
    if authority in {"identical", "replacement", "other"}:
        binding = copy.deepcopy(case.scope["binding"])
        if authority == "replacement":
            binding["revision"] += 1
        if authority == "other":
            binding["request_id"] = "other"
        # Genuine later/legacy writer, intentionally bypassing replay suppression.
        case.execute(
            "INSERT INTO coordinator_holds VALUES (?,?,?,?)",
            ("owner/repo", 1, binding["request_id"], json.dumps(binding, sort_keys=True)),
        )
    elif authority == "peer":
        case.execute(
            "INSERT INTO pauses VALUES (?,?,?,?,?)", ("owner/repo", 2, "a" * 40, "owner", 1.0)
        )
    elif authority == "history":
        case.execute(
            "INSERT INTO approvals VALUES (?,?,?,?,?)", ("owner/repo", 2, "a" * 40, 1, "old")
        )
    else:
        case.execute("INSERT INTO feature_contexts VALUES (?,?,?)", ("owner/repo", "peer/2", "old"))
    before = durable(case.controls)
    with case.pending("stale_replay"):
        case.renew()
    assert durable(case.controls) == before


@pytest.mark.parametrize("revision", [6, 7, 8])
def test_only_older_same_key_renewal_can_be_replaced(
    tmp_path: Any, monkeypatch: Any, revision: int
) -> None:
    case = Case(tmp_path, monkeypatch)
    old = {**case.scope["binding"], "revision": revision}
    case.execute(
        "INSERT INTO coordinator_renewals VALUES (?,?,?,?,?,?)",
        ("owner/repo", 1, "request-1", json.dumps(old, sort_keys=True), "1" * 40, "1" * 64),
    )
    case.refresh()
    before = durable(case.controls)
    if revision < 7:
        assert case.renew()["status"] == "committed"
    else:
        with case.pending("legacy_outcome" if revision == 7 else "authority_conflict"):
            case.renew()
        assert durable(case.controls) == before


@pytest.mark.parametrize(
    "stage", ["renewal", "hold", "receipt", "receipt-readback", "authority-readback", "commit"]
)
def test_every_transaction_failure_rolls_back_all_changes(
    tmp_path: Any, monkeypatch: Any, stage: str
) -> None:
    case = Case(tmp_path, monkeypatch)
    before = durable(case.controls)

    def inject(db: Any, sql: str) -> None:
        prefixes = {
            "renewal": "INSERT INTO coordinator_renewals",
            "hold": "DELETE FROM coordinator_holds",
            "receipt": "INSERT INTO coordinator_checked_renewals",
        }
        if stage in prefixes and sql.startswith(prefixes[stage]):
            message = "injected failure after actual write"
            raise sqlite3.OperationalError(message)
        if sql.startswith("INSERT INTO coordinator_checked_renewals"):
            if stage == "receipt-readback":
                db.execute("UPDATE coordinator_checked_renewals SET receipt_sha256=?", ("0" * 64,))
            elif stage == "authority-readback":
                db.execute(
                    "INSERT INTO pauses VALUES (?,?,?,?,?)",
                    ("owner/repo", 2, "a" * 40, "injected", 1.0),
                )

    def fail_commit(db: Any) -> None:
        message = "injected failure before commit"
        raise sqlite3.OperationalError(message)

    with monkeypatch.context() as patch:
        boundary(case, patch, execute=inject, commit=fail_commit if stage == "commit" else None)
        code = (
            "receipt_conflict"
            if stage == "receipt-readback"
            else "stale_authority"
            if stage == "authority-readback"
            else "database_unavailable"
        )
        with case.pending(code):
            case.renew()
    assert durable(case.controls) == before


def test_busy_writer_refuses_without_waiting_or_authority_change(
    tmp_path: Any, monkeypatch: Any
) -> None:
    case = Case(tmp_path, monkeypatch)
    before = durable(case.controls)
    db = sqlite3.connect(tmp_path / "review_control.sqlite3", timeout=0)
    try:
        db.execute("BEGIN IMMEDIATE")
        with case.pending("busy"):
            case.renew()
    finally:
        db.rollback()
        db.close()
    assert durable(case.controls) == before


def test_second_writer_cannot_enter_between_cas_and_hold_delete(
    tmp_path: Any, monkeypatch: Any
) -> None:
    case = Case(tmp_path, monkeypatch)
    acquired, attempted = threading.Event(), threading.Event()
    results = []
    real_connect = sqlite3.connect

    def writer() -> None:
        assert acquired.wait(1)
        db = real_connect(tmp_path / "review_control.sqlite3", timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                db.execute("BEGIN IMMEDIATE")
            results.append("blocked")
        finally:
            db.close()
            attempted.set()

    def intercept(db: Any, sql: str) -> None:
        if sql == "BEGIN IMMEDIATE":
            acquired.set()
            assert attempted.wait(1)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        with monkeypatch.context() as patch:
            boundary(case, patch, execute=intercept)
            assert case.renew()["status"] == "committed"
    finally:
        acquired.set()
        thread.join(2)
    assert not thread.is_alive()
    assert results == ["blocked"]


def test_two_identical_callers_have_one_immutable_transition(
    tmp_path: Any, monkeypatch: Any
) -> None:
    case = Case(tmp_path, monkeypatch)
    barrier = threading.Barrier(3)
    results = []

    def caller() -> None:
        barrier.wait(timeout=2)
        try:
            results.append(case.renew()["status"])
        except case.controls.CoordinatorAuthorityPending as error:
            results.append(error.code)

    threads = [threading.Thread(target=caller) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        barrier.wait(timeout=2)
    finally:
        for thread in threads:
            thread.join(3)
    assert all(not thread.is_alive() for thread in threads)
    assert results.count("committed") == 1
    assert set(results) <= {"committed", "replayed", "busy"}
    assert case.renew()["status"] == "replayed"
    with case.controls.connect() as db:
        assert db.execute("SELECT count(*) FROM coordinator_checked_renewals").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM coordinator_renewals").fetchone()[0] == 1


@pytest.mark.parametrize("location", ["renewal", "receipt"])
def test_deep_persisted_json_is_typed_pending(
    tmp_path: Any, monkeypatch: Any, location: str
) -> None:
    case = Case(tmp_path, monkeypatch)
    nested = "[" * 2000 + "0" + "]" * 2000
    if location == "renewal":
        binding = (
            '{"request_id":"other","repository":"owner/repo","revision":1,"opaque":' + nested + "}"
        )
        case.execute(
            "INSERT INTO coordinator_renewals VALUES (?,?,?,?,?,?)",
            ("owner/repo", 2, "other", binding, "a" * 40, "a" * 64),
        )
        case.refresh()
    else:
        case.renew()
        case.execute("UPDATE coordinator_checked_renewals SET receipt_json=?", (nested,))
    before = durable(case.controls)
    with case.pending("budget_exceeded"):
        case.renew()
    assert durable(case.controls) == before


@pytest.mark.parametrize("column", ["transition_sha256", "receipt_sha256", "created"])
def test_all_receipt_cells_are_capped_before_decode(
    tmp_path: Any, monkeypatch: Any, column: str
) -> None:
    case = Case(tmp_path, monkeypatch)
    case.renew()
    case.execute(f"UPDATE coordinator_checked_renewals SET {column}=?", ("x" * 262145,))
    before = durable(case.controls)
    real_connect = sqlite3.connect

    def limited_text(value: bytes) -> str:
        assert len(value) <= 16384  # Oversized SQL TEXT must not reach Python decoding.
        return value.decode()

    def connect(*args: Any, **kwargs: Any) -> Any:
        db = real_connect(*args, **kwargs)
        if kwargs.get("uri"):
            db.text_factory = limited_text
        return db

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        with case.pending("receipt_conflict"):
            case.renew()
    assert durable(case.controls) == before


@pytest.mark.parametrize("field", ["state", "head", "branch", "task", "proof"])
def test_consumed_public_identities_reject_subclasses_before_comparison(
    tmp_path: Any, monkeypatch: Any, field: str
) -> None:
    case = Case(tmp_path, monkeypatch)

    class Hostile(str):
        __slots__ = ()

        def __eq__(self, other: object) -> bool:
            message = "A non-built-in identity must not be compared"
            raise AssertionError(message)

        __hash__ = str.__hash__

    if field in {"state", "head", "branch"}:
        container, key = (
            (case.pr, "state")
            if field == "state"
            else (case.pr["head"], "sha" if field == "head" else "ref")
        )
        container[key] = Hostile(container[key])
    before = durable(case.controls)
    if field == "task":
        invoke = partial(
            case.controls.renew_coordinator_checked,
            case.gh,
            case.pr,
            case.scope["binding"],
            Hostile("1" * 64),
            scope=case.scope,
            expected_authority=case.expected,
            proof_sha256=case.proof,
            caller_binding=case.caller,
        )
    else:
        invoke = partial(
            case.renew, **({"proof_sha256": Hostile(case.proof)} if field == "proof" else {})
        )
    with pytest.raises(ValueError, match="Invalid checked coordinator contract"):
        invoke()
    assert durable(case.controls) == before


@pytest.mark.parametrize("progress", [False, True])
def test_deterministic_deadline_and_real_sqlite_progress_interrupt(
    tmp_path: Any, monkeypatch: Any, progress: bool
) -> None:
    case = Case(tmp_path, monkeypatch)
    helper = case.controls._coordinator_authority()
    before = durable(case.controls)

    class Clock:
        calls = 0
        expired = False

        def monotonic(self) -> float:
            self.calls += 1
            return 3.0 if self.expired or (progress and self.calls > 1) else 0.0

    clock = Clock()

    def intercept(db: Any, sql: str) -> None:
        if progress and sql == "BEGIN IMMEDIATE":
            db.execute(
                "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<10000) "
                "SELECT sum(x) FROM n"
            ).fetchone()
        if not progress and sql.startswith("INSERT INTO coordinator_renewals"):
            clock.expired = True

    with monkeypatch.context() as patch:
        patch.setattr(helper, "time", clock)
        patch.setattr(helper.snapshot, "time", clock)
        boundary(case, patch, execute=intercept)
        with case.pending("budget_exceeded"):
            case.renew()
    assert clock.calls >= 2
    assert durable(case.controls) == before
