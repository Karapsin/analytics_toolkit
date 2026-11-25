"""Authenticate ordered partial review outcomes independently of the mutable index."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from agent_tools import task_context

AUTHENTICATION = "_journal_authentication"
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


def _iterations() -> Any:
    from agent_tools import review_iterations  # noqa: PLC0415 - journal orchestration cycle.

    return review_iterations


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _data(state: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if key != AUTHENTICATION}


def _log_path(target: Path) -> Path:
    return target.with_name(target.stem + ".auth.jsonl")


def _records(root: Path, target: Path) -> list[dict[str, Any]]:
    path = _log_path(target)
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink() or not path.is_file():
        msg = "Unsafe signed review journal checkpoint log"
        raise ValueError(msg)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    iterations = _iterations()
    history = iterations.history(root)
    results = [attempt["result"] for attempt in history["attempts"] if "result" in attempt]
    task_id = task_context.read(root)["task_id"]
    previous, legacy = EMPTY_DIGEST, None
    fields = {
        "version",
        "kind",
        "task_id",
        "sequence",
        "previous_checkpoint_sha256",
        "state_sha256",
        "aggregate_history_count",
        "aggregate_history_sha256",
        "legacy_wave_ids",
    }
    for index, receipt in enumerate(records, 1):
        iterations._local().verify_signature(root, receipt)  # noqa: SLF001 - shared local signer.
        payload = receipt["payload"]
        count = payload.get("aggregate_history_count")
        if (
            set(payload) != fields
            or type(payload.get("version")) is not int
            or payload["version"] != 1
            or payload.get("kind") != "local-review-journal-checkpoint"
            or payload.get("task_id") != task_id
            or type(payload.get("sequence")) is not int
            or payload["sequence"] != index
            or payload.get("previous_checkpoint_sha256") != previous
            or not isinstance(payload.get("state_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["state_sha256"])
            or type(count) is not int
            or not 0 <= count <= len(results)
            or payload.get("aggregate_history_sha256")
            != iterations.findings_digest(results[:count])
            or not isinstance(payload.get("legacy_wave_ids"), list)
            or not all(isinstance(item, str) for item in payload["legacy_wave_ids"])
            or len(set(payload["legacy_wave_ids"])) != len(payload["legacy_wave_ids"])
            or (legacy is not None and payload["legacy_wave_ids"] != legacy)
        ):
            msg = "Review journal checkpoint changed its signed predecessor or aggregate history"
            raise ValueError(msg)
        previous, legacy = _digest(receipt), payload["legacy_wave_ids"]
    return records


def authenticate(root: Path, target: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Authenticate current order, recovering only an already signed pending rename."""
    records = _records(root, target)
    value = _data(state)
    if not records:
        return {
            **value,
            AUTHENTICATION: {
                "authenticated": False,
                "legacy_wave_ids": [wave["id"] for wave in value["waves"]],
            },
        }
    payload = records[-1]["payload"]
    if _digest(value) != payload["state_sha256"]:
        pending = target.with_suffix(".pending.json")
        if (
            pending.is_symlink()
            or not pending.is_file()
            or hashlib.sha256(pending.read_bytes()).hexdigest() != payload["state_sha256"]
        ):
            msg = "Review journal order differs from its latest signed checkpoint"
            raise ValueError(msg)
        value = json.loads(pending.read_text(encoding="utf-8"))
        pending.replace(target)
    legacy = payload["legacy_wave_ids"]
    if [wave["id"] for wave in value["waves"][: len(legacy)]] != legacy:
        msg = "Review journal changed its legacy audit prefix"
        raise ValueError(msg)
    return {**value, AUTHENTICATION: {"authenticated": True, "legacy_wave_ids": legacy}}


def _append_only(previous: dict[str, Any], state: dict[str, Any]) -> None:
    before, after = previous["waves"], state["waves"]
    if len(after) < len(before):
        msg = "Review journal cannot delete authenticated waves"
        raise ValueError(msg)
    for index, old in enumerate(before):
        new = after[index]
        if old == new:
            continue
        if index != len(before) - 1 or len(after) != len(before) or old["complete"]:
            msg = "Review journal cannot reorder or rewrite authenticated waves"
            raise ValueError(msg)
        if (
            {key: value for key, value in old.items() if key not in {"outcomes", "complete"}}
            != {key: value for key, value in new.items() if key not in {"outcomes", "complete"}}
            or new["outcomes"][: len(old["outcomes"])] != old["outcomes"]
            or len(new["outcomes"]) < len(old["outcomes"])
            or type(new.get("complete")) is not bool
        ):
            msg = "Review journal cannot rewrite signed partial outcome history"
            raise ValueError(msg)


def persist(root: Path, target: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Append one signed checkpoint before atomically publishing the matching index."""
    if target.is_symlink():
        msg = "Unsafe parallel review journal"
        raise ValueError(msg)
    previous = json.loads(target.read_text()) if target.exists() else {"version": 3, "waves": []}
    previous = authenticate(root, target, previous)
    value = _data(state)
    _append_only(previous, value)
    records = _records(root, target)
    iterations = _iterations()
    history = iterations.history(root)
    results = [attempt["result"] for attempt in history["attempts"] if "result" in attempt]
    legacy = (
        records[-1]["payload"]["legacy_wave_ids"]
        if records
        else [wave["id"] for wave in previous["waves"]]
    )
    data = _canonical(value)
    payload = {
        "version": 1,
        "kind": "local-review-journal-checkpoint",
        "task_id": task_context.read(root)["task_id"],
        "sequence": len(records) + 1,
        "previous_checkpoint_sha256": _digest(records[-1]) if records else EMPTY_DIGEST,
        "state_sha256": hashlib.sha256(data).hexdigest(),
        "aggregate_history_count": len(results),
        "aggregate_history_sha256": iterations.findings_digest(results),
        "legacy_wave_ids": legacy,
    }
    pending = target.with_suffix(".pending.json")
    if pending.is_symlink():
        msg = "Unsafe pending review journal checkpoint"
        raise ValueError(msg)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix="journal-", delete=False) as output:
        temporary = Path(output.name)
        output.write(data)
    temporary.replace(pending)
    with tempfile.TemporaryDirectory(prefix="atk-review-journal-") as temporary_job:
        receipt = iterations._local()._sign_payload(root, Path(temporary_job), payload)  # noqa: SLF001
    path = _log_path(target)
    if path.is_symlink():
        msg = "Unsafe signed review journal checkpoint log"
        raise ValueError(msg)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(descriptor, "ab") as output:
        output.write(_canonical(receipt) + b"\n")
        output.flush()
        os.fsync(output.fileno())
    pending.replace(target)
    return {"authenticated": True, "legacy_wave_ids": legacy}


def approval_state(state: dict[str, Any]) -> dict[str, Any]:
    """Unsealed legacy observations remain audit or diagnostic evidence only."""
    proof = state.get(AUTHENTICATION, {})
    legacy = set(proof.get("legacy_wave_ids", []))
    waves = (
        [wave for wave in state["waves"] if wave["id"] not in legacy]
        if proof.get("authenticated") is True
        else []
    )
    return {**state, "waves": waves}
