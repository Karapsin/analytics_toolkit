"""Protected fixed-scope entrypoints; no network or implicit schema migration."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import coordinator_contract as contract
from . import coordinator_snapshot as snapshot
from . import coordinator_transition as transition

CoordinatorAuthorityPending = contract.CoordinatorAuthorityPending


@contextmanager
def connection(*, write: bool) -> Iterator[tuple[sqlite3.Connection, float]]:
    directory = os.environ.get("AGENT_STATE_DIR")
    if not directory:
        contract.pending("database_unavailable")
    uri = (Path(directory).absolute() / "review_control.sqlite3").as_uri()
    end = time.monotonic() + 2
    db = None
    try:
        db = sqlite3.connect(uri + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=0)
        db.execute("PRAGMA busy_timeout=0")
        if not write:
            db.execute("PRAGMA query_only=ON")
        db.set_progress_handler(lambda: int(time.monotonic() >= end), 1000)
        yield db, end
    except sqlite3.Error as error:
        code = getattr(error, "sqlite_errorcode", None)
        if (type(code) is int and code & 255 in {5, 6}) or (
                code is None and str(error) in {"database is locked", "database table is locked"}):
            contract.pending("busy")
        if code == 9 or (code is None and str(error) == "interrupted") or time.monotonic() >= end:
            contract.pending("budget_exceeded")
        contract.pending("database_unavailable")
    finally:
        if db is not None:
            db.set_progress_handler(None, 0)
            db.close()  # Closing also rolls back every unsuccessful transition.


def prerequisite_authority_snapshot(repository: str, scope: dict[str, Any]) -> dict[str, Any]:
    scope = contract.clone(scope, contract.SCOPE_BYTES)
    contract.validate_scope(repository, scope)
    with connection(write=False) as (db, end):
        db.execute("BEGIN")
        snapshot.schema(db, end)
        value = snapshot.collect(db, scope, end)
        # Snapshot may describe barriers, but must have the receipt-anchored own hold.
        holds = snapshot.records(value, "coordinator_holds", scope["own"]["number"])
        matching = [row for row in holds if row["request"] == scope["binding"]["request_id"]
                    and row["binding"] == contract.legacy(scope["binding"])]
        if len(matching) != 1:
            contract.pending("stale_hold")
        snapshot.deadline(end)
        return value


def renew_coordinator_checked(
    gh: Any, pr: dict[str, Any], binding: dict[str, Any], task_digest: str, *,
    scope: dict[str, Any], expected_authority: dict[str, Any], proof_sha256: str,
    caller_binding: dict[str, Any],
) -> dict[str, Any]:
    scope = contract.clone(scope, contract.SCOPE_BYTES)
    binding = contract.clone(binding, 16384)
    caller_binding = contract.clone(caller_binding, 8192)
    expected_authority = contract.clone(expected_authority, contract.SNAPSHOT_BYTES, raw_strings=True)
    contract.validate_scope(gh.repository, scope)
    contract.hex_value(task_digest)
    contract.require(type(pr) is dict and type(pr.get("state")) is str and type(pr.get("number")) is int
                     and pr["number"] == scope["own"]["number"] and pr.get("state") == "open")
    head = pr.get("head")
    contract.require(type(head) is dict and type(head.get("sha")) is str and type(head.get("ref")) is str
                     and head.get("sha") == scope["own"]["head"]
                     and head.get("ref") == scope["own"]["branch"])
    contract.require(binding == scope["binding"] and type(binding) is dict
                     and task_digest == scope["own"]["task_digest"] and type(task_digest) is str)
    contract.validate_caller(scope, proof_sha256, caller_binding)
    snapshot.validate(expected_authority, scope)
    lengths = (len(contract.bounded(scope, contract.SCOPE_BYTES)),
               len(contract.bounded(expected_authority, contract.SNAPSHOT_BYTES, raw_strings=True)),
               len(contract.bounded(caller_binding, 8192)), len(contract.legacy(binding).encode()), len(proof_sha256))
    if sum(lengths) > 12 * 1024 * 1024:
        contract.pending("budget_exceeded")
    with connection(write=True) as (db, end):
        return transition.renew(db, scope, expected_authority, proof_sha256, caller_binding, end)
