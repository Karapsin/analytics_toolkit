"""Checked authority observes whole selected histories and fails closed."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest

from tests.repository._support.coordinator_authority import Case, canonical, digest, durable


def test_snapshot_preserves_raw_histories_and_explicit_absence(
    tmp_path: Any, monkeypatch: Any
) -> None:
    case = Case(tmp_path, monkeypatch)
    case.execute(
        "INSERT INTO corrections VALUES (?,?,?,?,?,?,?)",
        ("owner/repo", 2, "a" * 40, "b" * 64, '  old "reason" ', "\nchanges", 0.0),
    )
    case.execute(
        "INSERT INTO approvals VALUES (?,?,?,?,?)", ("owner/repo", 2, "a" * 40, 10, "old callback")
    )
    case.execute("INSERT INTO features VALUES (?,?,?)", ("owner/repo", "peer/2", "old-context"))
    case.execute(
        "INSERT INTO feature_contexts VALUES (?,?,?)", ("owner/repo", "peer/2", "retained-context")
    )
    before = durable(case.controls)
    value = case.controls.prerequisite_authority_snapshot(case.gh.repository, case.scope)
    assert durable(case.controls) == before
    assert len(value["tables"]) == 9
    corrections = next(t for t in value["tables"] if t["name"] == "corrections")["rows"]
    assert corrections[0][-3:] == [
        ["text", '  old "reason" '],
        ["text", "\nchanges"],
        ["real", "0x0.0p+0"],
    ]
    assert next(t for t in value["tables"] if t["name"] == "approvals")["rows"][0][3] == [
        "integer",
        "10",
    ]
    assert next(t for t in value["tables"] if t["name"] == "vetoes")["rows"] == []
    assert value["sha256"] == digest({k: v for k, v in value.items() if k != "sha256"})
    case.refresh()
    assert case.renew()["status"] == "committed"  # Actual old-head history is nonblocking.


@pytest.mark.parametrize(
    "change", ["omit", "add", "duplicate", "reorder", "branch", "receipt", "inner-hash", "plan"]
)
def test_peer_selection_is_anchored_to_persisted_full_receipt(
    tmp_path: Any, monkeypatch: Any, change: str
) -> None:
    case = Case(tmp_path, monkeypatch)
    value = copy.deepcopy(case.scope)
    if change == "omit":
        value["peers"].pop()
    elif change == "add":
        value["peers"].append({**value["peers"][-1], "number": 4})
    elif change == "duplicate":
        value["peers"][-1] = value["peers"][0]
    elif change == "reorder":
        value["peers"].reverse()
    elif change == "branch":
        value["peers"][0]["branch"] = "changed"
    elif change == "receipt":
        receipt = value["review_receipt"]
        receipt["prerequisites"].pop()
        receipt["sha256"] = digest({k: v for k, v in receipt.items() if k != "sha256"})
        value["binding"]["prerequisite_review_digest"] = digest(receipt)
        value["peers"].pop()
    elif change == "inner-hash":
        value["binding"]["prerequisite_review_digest"] = value["review_receipt"]["sha256"]
    else:
        value["review_receipt"]["binding"]["context_digest"] = "0" * 64
    before = durable(case.controls)
    with pytest.raises((ValueError, case.controls.CoordinatorAuthorityPending)):
        case.controls.prerequisite_authority_snapshot(case.gh.repository, value)
    assert durable(case.controls) == before


MUTATIONS = [
    (
        "coordinator_holds",
        "INSERT INTO coordinator_holds VALUES (?,?,?,?)",
        ("owner/repo", 2, "other", "{}"),
    ),
    (
        "coordinator_renewals",
        "INSERT INTO coordinator_renewals VALUES (?,?,?,?,?,?)",
        (
            "owner/repo",
            2,
            "other",
            '{"repository":"owner/repo","request_id":"other","revision":1}',
            "a" * 40,
            "a" * 64,
        ),
    ),
    ("pauses", "INSERT INTO pauses VALUES (?,?,?,?,?)", ("owner/repo", 2, "a" * 40, "owner", 1.0)),
    ("vetoes", "INSERT INTO vetoes VALUES (?,?,?,?)", ("owner/repo", 2, "a" * 40, 1.0)),
    (
        "approvals",
        "INSERT INTO approvals VALUES (?,?,?,?,?)",
        ("owner/repo", 2, "a" * 40, 1, "callback"),
    ),
    (
        "feature_amendments",
        "INSERT INTO feature_amendments VALUES (?,?,?,?)",
        ("owner/repo", 2, "a" * 40, 1.0),
    ),
    (
        "corrections",
        "INSERT INTO corrections VALUES (?,?,?,?,?,?,?)",
        ("owner/repo", 2, "a" * 40, "a" * 64, "old", "full", 1.0),
    ),
    ("features", "INSERT INTO features VALUES (?,?,?)", ("owner/repo", "peer/2", "history")),
    (
        "feature_contexts",
        "INSERT INTO feature_contexts VALUES (?,?,?)",
        ("owner/repo", "peer/2", "history"),
    ),
]


@pytest.mark.parametrize("mutation", MUTATIONS)
@pytest.mark.parametrize("action", ["insert", "delete", "update"])
def test_every_selected_table_change_invalidates_cas(
    tmp_path: Any, monkeypatch: Any, mutation: tuple[Any, ...], action: str
) -> None:
    table, sql, parameters = mutation
    case = Case(tmp_path, monkeypatch)
    if action == "insert":
        case.execute(sql, parameters)
    else:
        case.execute(sql, parameters)
        case.refresh()
        case.execute(
            f"DELETE FROM {table}"
            if action == "delete"
            else f"UPDATE {table} SET repository='changed'"
        )
    before = durable(case.controls)
    with case.pending("stale_authority"):
        case.renew()
    assert durable(case.controls) == before


@pytest.mark.parametrize("own", [False, True])
@pytest.mark.parametrize(
    "authority", ["hold", "pause", "amendment", "veto", "correction", "renewal"]
)
def test_current_barriers_and_allowed_own_correction(
    tmp_path: Any, monkeypatch: Any, own: bool, authority: str
) -> None:
    case = Case(tmp_path, monkeypatch)
    target = case.scope["own"] if own else case.scope["peers"][0]
    repo, n, head, task = (
        case.gh.repository,
        target["number"],
        target["head"],
        target["task_digest"],
    )
    statements = {
        "hold": ("INSERT INTO coordinator_holds VALUES (?,?,?,?)", (repo, n, "other", "{}")),
        "pause": (
            "INSERT INTO pauses VALUES (?,?,?,?,?)",
            (repo, n, "a" * 40, "owner question", 1.0),
        ),
        "amendment": ("INSERT INTO feature_amendments VALUES (?,?,?,?)", (repo, n, "a" * 40, 1.0)),
        "veto": ("INSERT INTO vetoes VALUES (?,?,?,?)", (repo, n, head, 1.0)),
        "correction": (
            "INSERT INTO corrections VALUES (?,?,?,?,?,?,?)",
            (repo, n, head, task, "reason", "full changes", 1.0),
        ),
        "renewal": (
            "INSERT INTO coordinator_renewals VALUES (?,?,?,?,?,?)",
            (
                repo,
                n,
                "other",
                '{"repository":"owner/repo","request_id":"other","revision":1}',
                head,
                task,
            ),
        ),
    }
    case.execute(*statements[authority])
    case.refresh()
    before = durable(case.controls)
    if own and authority in {"correction", "renewal"}:
        assert case.renew()["status"] == "committed"
        with case.controls.connect() as db:
            table = "corrections" if authority == "correction" else "coordinator_renewals"
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= 1
    else:
        with case.pending("stale_hold" if own and authority == "hold" else "authority_conflict"):
            case.renew()
        assert durable(case.controls) == before


@pytest.mark.parametrize("table", ["corrections", "coordinator_renewals"])
def test_same_head_other_task_cannot_renew(tmp_path: Any, monkeypatch: Any, table: str) -> None:
    case = Case(tmp_path, monkeypatch)
    if table == "corrections":
        case.execute(
            "INSERT INTO corrections VALUES (?,?,?,?,?,?,?)",
            ("owner/repo", 1, "1" * 40, "0" * 64, "reason", "full", 1.0),
        )
    else:
        case.execute(
            "INSERT INTO coordinator_renewals VALUES (?,?,?,?,?,?)",
            ("owner/repo", 1, "other", "{}", "1" * 40, "0" * 64),
        )
    case.refresh()
    with case.pending("authority_conflict"):
        case.renew()


@pytest.mark.parametrize(
    "fault", ["receipt-trigger", "authority-trigger", "extra-column", "no-unique", "view"]
)
def test_unsupported_schema_never_changes_authority(
    tmp_path: Any, monkeypatch: Any, fault: str
) -> None:
    case = Case(tmp_path, monkeypatch)
    if fault == "receipt-trigger":
        case.execute(
            "CREATE TRIGGER bad AFTER INSERT ON coordinator_checked_renewals "
            "BEGIN DELETE FROM pauses; END"
        )
    elif fault == "authority-trigger":
        case.execute(
            "CREATE TRIGGER bad AFTER DELETE ON coordinator_holds BEGIN DELETE FROM vetoes; END"
        )
    elif fault == "extra-column":
        case.execute("ALTER TABLE approvals ADD COLUMN surprise TEXT")
    elif fault == "view":
        with case.controls.connect() as db:
            db.execute("ALTER TABLE approvals RENAME TO approvals_original")
            db.execute("CREATE VIEW approvals AS SELECT * FROM approvals_original")
    else:
        with case.controls.connect() as db:
            ddl = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='coordinator_checked_renewals'"
            ).fetchone()[0]
            db.execute("DROP TABLE coordinator_checked_renewals")
            db.execute(ddl.replace("NOT NULL UNIQUE", "NOT NULL"))
    before = durable(case.controls)
    with case.pending("unsupported_schema"):
        case.renew()
    assert durable(case.controls) == before


def test_snapshot_does_not_create_missing_database(tmp_path: Any, monkeypatch: Any) -> None:
    case = Case(tmp_path, monkeypatch)
    absent = tmp_path / "absent"
    monkeypatch.setenv("AGENT_STATE_DIR", str(absent))
    with case.pending("database_unavailable"):
        case.controls.prerequisite_authority_snapshot(case.gh.repository, case.scope)
    assert not absent.exists()


def test_unselected_rows_are_outside_the_local_transaction_scope(
    tmp_path: Any, monkeypatch: Any
) -> None:
    case = Case(tmp_path, monkeypatch)
    case.execute(
        "INSERT INTO pauses VALUES (?,?,?,?,?)", ("owner/repo", 999, "f" * 40, "other", 1.0)
    )
    case.execute("INSERT INTO features VALUES (?,?,?)", ("owner/repo", "unselected", "history"))
    assert case.renew()["status"] == "committed"


@pytest.mark.parametrize("value", [True, [], {}, 1.0, "7", -1])
def test_strict_identity_types(tmp_path: Any, monkeypatch: Any, value: Any) -> None:
    case = Case(tmp_path, monkeypatch)
    case.scope["binding"]["revision"] = value
    with pytest.raises(ValueError, match="Invalid checked coordinator contract"):
        case.renew()


def test_encoding_reference_is_utf8_canonical_not_legacy_ascii() -> None:
    assert canonical({"reason": "é"}) == b'{"reason":"\xc3\xa9"}'
    assert canonical({"reason": "é"}) != json.dumps({"reason": "é"}, sort_keys=True).encode()


def test_maximum_peer_set_and_rejection_of_seventeenth(tmp_path: Any, monkeypatch: Any) -> None:
    case = Case(tmp_path, monkeypatch, 16)
    assert case.renew()["status"] == "committed"
    invalid = case.make_scope(17)
    with pytest.raises(ValueError, match="Invalid checked coordinator contract"):
        case.controls.prerequisite_authority_snapshot(case.gh.repository, invalid)


@pytest.mark.parametrize("key", [1, False, None, ("tuple",)])
def test_opaque_metadata_never_coerces_dictionary_keys(
    tmp_path: Any, monkeypatch: Any, key: Any
) -> None:
    case = Case(tmp_path, monkeypatch)
    case.scope["review_receipt"]["execution"]["identity"]["opaque"] = {key: "value"}
    before = durable(case.controls)
    with pytest.raises(ValueError, match="Invalid checked coordinator contract"):
        case.renew()
    assert durable(case.controls) == before


@pytest.mark.parametrize("kind", ["cycle", "depth", "nodes", "string", "subclass"])
def test_bounded_input_refuses_before_any_sqlite_mutation(
    tmp_path: Any, monkeypatch: Any, kind: str
) -> None:
    case = Case(tmp_path, monkeypatch)
    identity = case.scope["review_receipt"]["execution"]["identity"]
    if kind == "cycle":
        identity["opaque"] = identity
    elif kind == "depth":
        nested: Any = "leaf"
        for _ in range(25):
            nested = [nested]
        identity["opaque"] = nested
    elif kind == "nodes":
        identity["opaque"] = [None] * 200001
    elif kind == "string":
        identity["opaque"] = "é" * 32769
    else:

        class String(str):
            __slots__ = ()

        identity["opaque"] = String("value")
    before = durable(case.controls)
    with pytest.raises((ValueError, case.controls.CoordinatorAuthorityPending)):
        case.renew()
    assert durable(case.controls) == before


def test_canonical_exact_byte_boundary_and_finite_metadata(tmp_path: Any, monkeypatch: Any) -> None:
    case = Case(tmp_path, monkeypatch)
    helper = case.controls._coordinator_authority().contract
    assert helper.bounded("abcdef", 8) == b'"abcdef"'
    with case.pending("budget_exceeded"):
        helper.bounded("abcdefg", 8)
    with pytest.raises(ValueError, match="Invalid checked coordinator contract"):
        helper.bounded({"opaque": float("inf")}, 100)
    assert helper.bounded({"opaque": 1.5}, 100) == b'{"opaque":1.5}'


def test_sql_row_budget_checked_before_materializing_body(tmp_path: Any, monkeypatch: Any) -> None:
    case = Case(tmp_path, monkeypatch)
    case.execute(
        "INSERT INTO approvals VALUES (?,?,?,?,?)", ("owner/repo", 2, "a" * 40, 1, "x" * 262145)
    )
    before = durable(case.controls)
    with case.pending("budget_exceeded"):
        case.controls.prerequisite_authority_snapshot(case.gh.repository, case.scope)
    assert durable(case.controls) == before


def test_actual_table_collection_exact_row_limit(tmp_path: Any, monkeypatch: Any) -> None:
    case = Case(tmp_path, monkeypatch)
    # Exercise the row boundary independently from the separate deadline tests.
    helper = case.controls._coordinator_authority()
    clock = SimpleNamespace(monotonic=lambda: 0.0)
    monkeypatch.setattr(helper, "time", clock)
    monkeypatch.setattr(helper.snapshot, "time", clock)
    with case.controls.connect() as db:
        db.executemany(
            "INSERT INTO approvals VALUES (?,?,?,?,?)",
            [("owner/repo", 2, format(n, "040x"), 1, "history") for n in range(4096)],
        )
    value = case.controls.prerequisite_authority_snapshot(case.gh.repository, case.scope)
    table = next(t for t in value["tables"] if t["name"] == "approvals")
    assert len(table["rows"]) == 4096
    case.execute(
        "INSERT INTO approvals VALUES (?,?,?,?,?)", ("owner/repo", 2, "f" * 40, 1, "history")
    )
    before = durable(case.controls)
    with case.pending("budget_exceeded"):
        case.controls.prerequisite_authority_snapshot(case.gh.repository, case.scope)
    assert durable(case.controls) == before


def test_actual_sqlite_aggregate_snapshot_byte_boundary(tmp_path: Any, monkeypatch: Any) -> None:
    case = Case(tmp_path, monkeypatch)
    helper = case.controls._coordinator_authority()
    clock = SimpleNamespace(monotonic=lambda: 0.0)
    monkeypatch.setattr(helper, "time", clock)
    monkeypatch.setattr(helper.snapshot, "time", clock)
    payload = "x" * 250000  # Each row is permitted; only the aggregate exceeds 4MiB.
    with case.controls.connect() as db:
        db.executemany(
            "INSERT INTO approvals VALUES (?,?,?,?,?)",
            [("owner/repo", 2, format(n, "040x"), 1, payload) for n in range(16)],
        )
    value = case.controls.prerequisite_authority_snapshot(case.gh.repository, case.scope)
    assert len(next(t for t in value["tables"] if t["name"] == "approvals")["rows"]) == 16
    case.execute(
        "INSERT INTO approvals VALUES (?,?,?,?,?)", ("owner/repo", 2, "f" * 40, 1, payload)
    )
    before = durable(case.controls)
    with case.pending("budget_exceeded"):
        case.controls.prerequisite_authority_snapshot(case.gh.repository, case.scope)
    assert durable(case.controls) == before
