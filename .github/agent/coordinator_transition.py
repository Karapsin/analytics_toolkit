"""One control-database transaction and immutable checked renewal replay."""
from __future__ import annotations

import hashlib
import math
import sqlite3
import time
from typing import Any

from . import coordinator_contract as contract
from . import coordinator_snapshot as snapshot

BODY_KEYS = ("version", "operation", "repository", "number", "request_id", "binding_sha256",
             "scope_sha256", "pre_authority_sha256", "post_authority_sha256", "proof_sha256",
             "caller_binding", "transition_sha256")


def receipt_key(scope: dict[str, Any]) -> tuple[Any, ...]:
    return (scope["repository"], scope["own"]["number"], scope["binding"]["request_id"],
            hashlib.sha256(contract.legacy(scope["binding"]).encode()).hexdigest())


def body(scope: dict[str, Any], before: dict[str, Any], after: dict[str, Any],
         proof: str, caller: dict[str, Any]) -> dict[str, Any]:
    value = {"version": 1, "operation": contract.OPERATION, "repository": scope["repository"],
             "number": scope["own"]["number"], "request_id": scope["binding"]["request_id"],
             "binding_sha256": receipt_key(scope)[3], "scope_sha256": before["scope_sha256"],
             "pre_authority_sha256": before["sha256"], "post_authority_sha256": after["sha256"],
             "proof_sha256": proof, "caller_binding": caller}
    return dict(value, transition_sha256=contract.digest(value, 16384))


def read_receipt(db: sqlite3.Connection, scope: dict[str, Any]) -> tuple[Any, ...] | None:
    columns = ",".join(snapshot.RECEIPT_COLUMNS)
    where = "repository=? AND number=? AND request=? AND binding_sha256=?"
    # Gate every cell before fetching any potentially corrupt historical body.
    metadata = ",".join(f"typeof({c}),length(CAST({c} AS BLOB))" for c in snapshot.RECEIPT_COLUMNS)
    sizes = db.execute(f"SELECT {metadata} FROM {snapshot.RECEIPT_TABLE} WHERE {where}", receipt_key(scope)).fetchone()
    if sizes is None:
        return None
    kinds = ({"text"}, {"integer"}, {"text"}, {"text"}, {"text"}, {"text"}, {"text"}, {"real", "integer"})
    limits = (201, 10, 128, 64, 64, 16384, 64, 32)
    if any(sizes[2 * i] not in kind or type(sizes[2 * i + 1]) is not int
           or not 0 < sizes[2 * i + 1] <= limits[i] for i, kind in enumerate(kinds)):
        contract.pending("receipt_conflict")
    if sum(sizes[i] for i in range(1, len(sizes), 2)) > contract.ROW_BYTES:
        contract.pending("receipt_conflict")
    row = db.execute(f"SELECT {columns} FROM {snapshot.RECEIPT_TABLE} WHERE {where}", receipt_key(scope)).fetchone()
    if row is None:
        contract.pending("receipt_conflict")
    return tuple(row)


def verify_receipt(row: tuple[Any, ...], intended: dict[str, Any], scope: dict[str, Any]) -> None:
    try:
        contract.require(row[:4] == receipt_key(scope) and type(row[5]) is str)
        stored = contract.clone(contract.decode(row[5]), 16384)
        contract.keys(stored, BODY_KEYS)
        contract.require(stored == intended and contract.bounded(stored, 16384).decode() == row[5])
        contract.require(row[4] == stored["transition_sha256"] and row[6] == contract.digest(stored, 16384))
        contract.require(type(row[7]) in {int, float} and math.isfinite(row[7]))
    except (ValueError, TypeError, KeyError):
        contract.pending("receipt_conflict")


def result(value: dict[str, Any], scope: dict[str, Any], status: str) -> dict[str, Any]:
    names = ("transition_sha256", "scope_sha256", "pre_authority_sha256",
             "post_authority_sha256", "proof_sha256")
    return {"version": 1, "status": status, **{name: value[name] for name in names},
            "receipt_sha256": contract.digest(value, 16384),
            "caller_binding_sha256": contract.digest(value["caller_binding"], 8192),
            "renewal_head": scope["own"]["head"], "task_digest": scope["own"]["task_digest"]}


def write_renewal(db: sqlite3.Connection, scope: dict[str, Any], before: dict[str, Any]) -> None:
    binding, own = scope["binding"], scope["own"]
    row = (scope["repository"], own["number"], binding["request_id"], contract.legacy(binding),
           own["head"], own["task_digest"])
    prior = snapshot.own_renewal(before, scope)
    if prior is None:
        changed = db.execute("INSERT INTO coordinator_renewals VALUES (?,?,?,?,?,?)", row).rowcount
    else:
        columns = snapshot.TABLES["coordinator_renewals"]
        changed = db.execute(
            "UPDATE coordinator_renewals SET binding=?,head=?,digest=? WHERE "
            + " AND ".join(f"{name} IS ?" for name in columns),
            (*row[3:], *(prior[name] for name in columns)),
        ).rowcount
    removed = db.execute(
        "DELETE FROM coordinator_holds WHERE repository=? AND number=? AND request=? AND binding=?", row[:4],
    ).rowcount
    if changed != 1 or removed != 1:
        contract.pending("stale_hold")


def renew(db: sqlite3.Connection, scope: dict[str, Any], expected: dict[str, Any],
          proof: str, caller: dict[str, Any], end: float) -> dict[str, Any]:
    db.execute("BEGIN IMMEDIATE")
    snapshot.schema(db, end)
    stored = read_receipt(db, scope)
    if stored is None:
        current = snapshot.collect(db, scope, end)
        if current != expected:
            contract.pending("stale_authority")
    after = snapshot.post_state(expected, scope)
    intended = body(scope, expected, after, proof, caller)
    if stored is not None:
        verify_receipt(stored, intended, scope)
        current = snapshot.collect(db, scope, end)
        if current != after:
            contract.pending("stale_replay")
        snapshot.eligibility(current, scope, replay=True)
        db.rollback()  # Identical replay performs no write, including audit time.
        return result(intended, scope, "replayed")
    write_renewal(db, scope, current)
    if snapshot.collect(db, scope, end) != after:
        contract.pending("stale_authority")
    encoded = contract.bounded(intended, 16384).decode()
    created = time.time()
    if not math.isfinite(created):
        contract.pending("database_unavailable")
    row = (*receipt_key(scope), intended["transition_sha256"], encoded, contract.digest(intended, 16384), created)
    db.execute(f"INSERT INTO {snapshot.RECEIPT_TABLE} VALUES (?,?,?,?,?,?,?,?)", row)
    readback = read_receipt(db, scope)
    if readback != row:
        contract.pending("receipt_conflict")
    verify_receipt(readback, intended, scope)
    if snapshot.collect(db, scope, end) != after:
        contract.pending("stale_authority")
    snapshot.deadline(end)
    db.commit()
    return result(intended, scope, "committed")
