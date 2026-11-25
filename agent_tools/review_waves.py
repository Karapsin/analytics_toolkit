"""Bounded parallel discovery with persistent scoped outcomes and compact attestations."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_tools import review_chunks, review_clearances, review_journal, task_context

if TYPE_CHECKING:
    from collections.abc import Callable

PROCEDURE = "discovery-chunks-v4"
_JOURNAL_VERSION = 3
_LEGACY_WAVE_VERSION = 3
_WAVE_VERSION = 4
_SHA256_HEX_LENGTH = 64
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BatchContext:
    root: Path
    subject: dict[str, str]
    manifest: dict[str, Any]
    request: dict[str, Any]
    execute: Callable[[str], dict[str, Any]]


def _iterations() -> Any:
    from agent_tools import (  # noqa: PLC0415 -- avoid the orchestration import cycle.
        review_iterations,
    )

    return review_iterations


def _local() -> Any:
    return _iterations()._local()  # noqa: SLF001 - same trusted local provenance implementation.


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _prior_findings(
    iterations: Any, results: list[dict[str, Any]], subject: dict[str, str]
) -> list[str]:
    return iterations._findings(results, subject)  # noqa: SLF001 -- retained orchestration compatibility hook.


def _findings(children: list[dict[str, Any]]) -> list[str]:
    """Keep concrete evidence from partial/malformed verdicts without promoting coverage."""
    findings = []
    for child in children:
        raw = child["payload"]["verdict"].get("findings")
        if isinstance(raw, list):
            findings.extend(
                item for item in raw if isinstance(item, str) and re.match(r"^\[P[0-3]\]\s", item)
            )
    return list(dict.fromkeys(findings))


def _chunk_diagnostic(payload: dict[str, Any], known: list[dict[str, Any]]) -> str:
    verdict = payload["verdict"]
    reasons = []
    raw = verdict.get("findings")
    findings = _findings([{"payload": payload}])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        reasons.append("rejected malformed reviewer verdict: missing or invalid findings")
    if payload.get("coverage_complete") is not True:
        reasons.append("complete diff coverage missing")
    known_ids = {item["id"] for item in known}
    repeated = [
        "finding:" + hashlib.sha256(item.encode("utf-8")).hexdigest()
        for item in findings
        if "finding:" + hashlib.sha256(item.encode("utf-8")).hexdigest() in known_ids
    ]
    if repeated:
        reasons.append(
            "incomplete; repeated recorded finding IDs are not novel: " + ", ".join(repeated)
        )
    if verdict.get("approved") is True and findings:
        reasons.append("rejected verdict: approval conflicts with unresolved findings")
    if verdict.get("alignment") == "uncertain":
        reasons.append("rejected uncertain source assessment")
    if not reasons:
        reasons.append("rejected incomplete reviewer verdict")
    summary = verdict.get("summary")
    if isinstance(summary, str):
        reasons.append("Reviewer summary: " + summary)
    reasons.extend(findings)
    return "Chunk " + str(payload.get("chunk_id", "unknown")) + ": " + "\n".join(reasons)


def _path(root: Path) -> Path:
    return _local().directory(root) / ("waves-" + task_context.read(root)["task_id"] + ".json")


def _read(root: Path) -> dict[str, Any]:
    path = _path(root)
    if not path.exists():
        return {"version": _JOURNAL_VERSION, "waves": []}
    if path.is_symlink():
        msg = "Unsafe parallel review journal"
        raise ValueError(msg)
    state = json.loads(path.read_text())
    if state.get("version") != _JOURNAL_VERSION or not isinstance(state.get("waves"), list):
        msg = "Invalid parallel review journal"
        raise ValueError(msg)
    state = review_journal.authenticate(root, path, state)
    for wave in state["waves"]:
        review_chunks.validate_manifest(wave["subject"], wave["manifest"])
        if wave["plan_sha256"] != review_chunks.plan_digest(wave["manifest"]):
            msg = "Parallel review journal plan digest mismatch"
            raise ValueError(msg)
        for event in wave["outcomes"]:
            if "result" in event:
                verify_chunk(root, event["result"], wave["subject"], wave["manifest"], wave["id"])
    return state


def _save(root: Path, state: dict[str, Any]) -> None:
    path = _path(root)
    state[review_journal.AUTHENTICATION] = review_journal.persist(root, path, state)


def ledger_path(payload: dict[str, Any]) -> Path:
    path = Path(payload["coverage_ledger"])
    if path.is_symlink() or not path.is_file():
        msg = "Scoped review coverage ledger is missing or unsafe"
        raise ValueError(msg)
    if hashlib.sha256(path.read_bytes()).hexdigest() != payload.get("coverage_ledger_sha256"):
        msg = "Scoped review coverage ledger digest mismatch"
        raise ValueError(msg)
    return path


def verify_chunk(
    root: Path,
    receipt: dict[str, Any],
    subject: dict[str, str],
    manifest: dict[str, Any],
    wave_id: str,
) -> dict[str, Any]:
    iterations = _iterations()
    payload = iterations.verify_round(root, receipt)
    chunks = {chunk["id"]: chunk for chunk in manifest["chunks"]}
    chunk = chunks.get(payload.get("chunk_id"))
    if (
        any(payload[key] != subject[key] for key in iterations.SUBJECT_KEYS)
        or payload.get("review_procedure") not in iterations.PARALLEL_PROCEDURES
        or payload.get("review_scope") != "chunk"
        or payload.get("wave_id") != wave_id
        or payload.get("plan_sha256") != review_chunks.plan_digest(manifest)
        or chunk is None
        or payload.get("scope_sha256") != review_chunks.plan_digest(chunk)
    ):
        msg = "Scoped review changed its candidate, plan, wave or assignment"
        raise ValueError(msg)
    ledger_path(payload)
    return payload


def aggregate_payload(
    subject: dict[str, str],
    iteration: dict[str, Any],
    manifest: dict[str, Any],
    children: list[dict[str, Any]],
    coverage_complete: bool,  # noqa: FBT001 -- preserve the positional signed-payload builder ABI.
) -> dict[str, Any]:
    """Aggregate signed scoped observations; no child supplies whole-feature approval."""
    if not children:
        msg = "A review wave has no signed reviewer outcomes"
        raise ValueError(msg)
    iterations = _iterations()
    representative = children[-1]["payload"]
    findings = _findings(children)
    approved = (
        coverage_complete
        and not findings
        and not iteration["recorded_findings"]
        and not iteration["discovery_only"]
    )
    return {
        "version": 1,
        "kind": "local-review-round",
        **subject,
        **{key: value for key, value in representative.items() if key.startswith("reviewer_")},
        "review_procedure": PROCEDURE,
        "review_scope": "wave",
        "discovery_only": iteration["discovery_only"],
        "wave_id": iteration["wave_id"],
        "plan_sha256": review_chunks.plan_digest(manifest),
        "coverage_complete": coverage_complete,
        "execution_sha256": _digest(children),
        "prior_findings_sha256": iterations.findings_digest(iteration["prior_findings"]),
        "previous_rounds_sha256": iteration["previous_rounds_sha256"],
        "recorded_findings_sha256": iterations.findings_digest(iteration["recorded_findings"]),
        "verdict": {
            "approved": approved,
            "alignment": "aligned"
            if approved
            else "not_aligned"
            if coverage_complete
            else "uncertain",
            "summary": "Aggregate scoped review wave; all assignments complete."
            if coverage_complete
            else "Incomplete scoped review wave; no whole-candidate convergence.",
            "change_summary": "Aggregate of independent component and integration reviews.",
            "findings": findings,
        },
        "review_wave": {
            "version": _WAVE_VERSION,
            "manifest": manifest,
            "children": children,
            "iteration": iteration.get("chunk_iteration", iteration),
            "origins": [
                iteration.get("child_origins", {}).get(
                    child["payload"]["chunk_id"],
                    {
                        "subject": subject,
                        "manifest": manifest,
                        "wave_id": child["payload"]["wave_id"],
                        "iteration": iteration.get("chunk_iteration", iteration),
                    },
                )
                for child in children
            ],
        },
    }


def _wave_origins(payload: dict[str, Any], subject: dict[str, str]) -> list[dict[str, Any]]:
    proof = payload["review_wave"]
    return (
        proof.get("origins", [])
        if proof["version"] == _WAVE_VERSION
        else [
            {
                "subject": subject,
                "manifest": proof["manifest"],
                "wave_id": payload["wave_id"],
                "iteration": proof["iteration"],
            }
            for _ in proof["children"]
        ]
    )


def _verify_wave_child(
    root: Path,
    payload: dict[str, Any],
    manifest: dict[str, Any],
    child: dict[str, Any],
    origin: dict[str, Any],
) -> dict[str, Any]:
    item = verify_chunk(root, child, origin["subject"], origin["manifest"], origin["wave_id"])
    if payload["review_wave"]["version"] == _WAVE_VERSION:
        review_clearances.verify_origin(
            manifest, origin, item, discovery_only=payload["discovery_only"]
        )
        if manifest != origin["manifest"] and not _complete_chunk(
            item, origin["iteration"]["recorded_findings"]
        ):
            msg = "Only completed zero-new discovery clearances can be adopted"
            raise ValueError(msg)
    return item


def _verify_wave_children(
    root: Path,
    payload: dict[str, Any],
    manifest: dict[str, Any],
    origins: list[dict[str, Any]],
) -> set[str]:
    children = payload["review_wave"]["children"]
    if len(origins) != len(children):
        msg = "Every scoped clearance requires its original immutable context"
        raise ValueError(msg)
    seen_chunks, identities = set(), set()
    for child, origin in zip(children, origins):
        item = _verify_wave_child(root, payload, manifest, child, origin)
        context = origin["iteration"]
        if item["chunk_id"] in seen_chunks or item["reviewer_id"] in identities:
            msg = "Every scoped review requires a distinct assignment and fresh identity"
            raise ValueError(msg)
        if (
            item["recorded_findings_sha256"]
            != _iterations().findings_digest(context["recorded_findings"])
            or item["previous_rounds_sha256"] != context["previous_rounds_sha256"]
            or item["prior_findings_sha256"]
            != _iterations().findings_digest(context["prior_findings"])
            or item["discovery_only"] is not payload["discovery_only"]
        ):
            msg = "Scoped review does not bind its frozen wave context"
            raise ValueError(msg)
        seen_chunks.add(item["chunk_id"])
        identities.add(item["reviewer_id"])
    return seen_chunks


def verify_wave(root: Path, receipt: dict[str, Any], *, complete: bool = False) -> dict[str, Any]:
    """Verify the aggregate and every child without granting a child global authority."""
    _local().verify_signature(root, receipt)
    payload = receipt["payload"]
    proof = payload.get("review_wave", {})
    version = proof.get("version")
    if (
        version not in {_LEGACY_WAVE_VERSION, _WAVE_VERSION}
        or payload.get("review_scope") != "wave"
    ):
        msg = "A signed parallel review wave is required"
        raise ValueError(msg)
    manifest, children = proof["manifest"], proof["children"]
    subject = {key: payload[key] for key in _iterations().SUBJECT_KEYS}
    review_chunks.validate_manifest(subject, manifest)
    if payload["plan_sha256"] != review_chunks.plan_digest(manifest) or not children:
        msg = "Review wave plan binding is invalid"
        raise ValueError(msg)
    expected = {chunk["id"] for chunk in manifest["chunks"]}
    initial = proof["iteration"]
    origins = _wave_origins(payload, subject)
    seen_chunks = _verify_wave_children(root, payload, manifest, origins)
    findings = _findings(children)
    if (
        payload["execution_sha256"] != _digest(children)
        or payload["reviewer_id"] != children[-1]["payload"]["reviewer_id"]
        or payload["verdict"]["findings"] != findings
    ):
        msg = "Review wave is not the actual aggregate of its scoped outcomes"
        raise ValueError(msg)
    approved = (
        payload["coverage_complete"]
        and not findings
        and not initial["recorded_findings"]
        and (version == _LEGACY_WAVE_VERSION or not payload["discovery_only"])
    )
    alignment = (
        "aligned" if approved else "not_aligned" if payload["coverage_complete"] else "uncertain"
    )
    if (
        payload["verdict"].get("approved") is not approved
        or payload["verdict"].get("alignment") != alignment
    ):
        msg = "A child outcome cannot independently approve the complete feature"
        raise ValueError(msg)
    if (payload["coverage_complete"] or complete) and (
        seen_chunks != expected
        or not all(
            _complete_chunk(child["payload"], origin["iteration"]["recorded_findings"])
            for child, origin in zip(children, origins)
        )
    ):
        msg = "Incomplete component or integration coverage cannot certify a wave"
        raise ValueError(msg)
    if complete and payload["coverage_complete"] is not True:
        msg = "Incomplete review wave cannot converge"
        raise ValueError(msg)
    return payload


def _known(root: Path, subject: dict[str, str], state: dict[str, Any]) -> list[dict[str, Any]]:
    iterations = _iterations()
    results = [item["result"] for item in iterations.history(root)["attempts"] if "result" in item]
    results.extend(
        event["result"]
        for wave in state["waves"]
        for event in wave["outcomes"]
        if "result" in event
    )
    return iterations.recorded_findings(results, subject)


def _ordered_results(wave: dict[str, Any]) -> list[dict[str, Any]]:
    latest = {event["chunk_id"]: event["result"] for event in wave["outcomes"] if "result" in event}
    return [latest[chunk["id"]] for chunk in wave["manifest"]["chunks"] if chunk["id"] in latest]


def _complete_chunk(payload: dict[str, Any], known: list[dict[str, Any]]) -> bool:
    if _iterations()._complete_discovery(payload, known):  # noqa: SLF001 - shared completion contract.
        return True
    # A clean scoped result need not approve unresolved work in other chunks.
    return bool(known) and _iterations().classification(payload) == "clean"


def _start_wave(
    context: _BatchContext, plan_sha: str, iterations: Any, *, discovery_only: bool
) -> tuple[
    dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]
]:
    root, subject, manifest = context.root, context.subject, context.manifest
    state = _read(root)
    history = iterations.history(root)
    results = [item["result"] for item in history["attempts"] if "result" in item]
    known = _known(root, subject, state)
    selected = review_clearances.latest(
        state if discovery_only else review_journal.approval_state(state),
        manifest,
        discovery_only=discovery_only,
        complete=_complete_chunk,
    )
    cleared = {
        key
        for key, (child, origin) in selected.items()
        if _complete_chunk(child["payload"], origin["iteration"]["recorded_findings"])
        and not _findings([child])
    }
    missing = [chunk for chunk in manifest["chunks"] if chunk["id"] not in cleared]
    # Each retry has a fresh context; prior incomplete findings are now OPEN exclusions.
    wave_id = uuid.uuid4().hex
    iteration = {
        "attempt_id": uuid.uuid4().hex,
        "number": 1
        + sum(
            item["subject"] == subject
            and item["plan_sha256"] == plan_sha
            and item["discovery_only"] is discovery_only
            for item in state["waves"]
        ),
        "minimum_completed_rounds": iterations.MINIMUM_ROUNDS,
        "procedure": PROCEDURE,
        "discovery_only": discovery_only,
        "wave_id": wave_id,
        "instructions": iterations.INSTRUCTIONS,
        "prior_findings": _prior_findings(iterations, results, subject),
        "recorded_findings": known,
        "excluded_finding_ids": [item["id"] for item in known],
        "previous_rounds_sha256": iterations.findings_digest(results),
    }
    wave = {
        "id": wave_id,
        "subject": subject,
        "manifest": manifest,
        "plan_sha256": plan_sha,
        "discovery_only": discovery_only,
        "iteration": iteration,
        "outcomes": [],
        "complete": False,
        "scheduled_chunk_ids": [chunk["id"] for chunk in missing],
        "retired_chunk_ids": [
            chunk["id"] for chunk in manifest["chunks"] if chunk["id"] in cleared
        ],
    }
    state["waves"].append(wave)
    _save(root, state)
    return state, history, results, wave, missing


def _used_identities(results: list[dict[str, Any]], state: dict[str, Any]) -> set[str]:
    identities = {result["payload"]["reviewer_id"] for result in results}
    identities.update(
        event["result"]["payload"]["reviewer_id"]
        for item in state["waves"]
        for event in item["outcomes"]
        if "result" in event
    )
    return identities


def _accept_worker(
    context: _BatchContext,
    wave: dict[str, Any],
    chunk: dict[str, Any],
    receipt: dict[str, Any],
    used_identities: set[str],
) -> None:
    item = verify_chunk(context.root, receipt, context.subject, context.manifest, wave["id"])
    if item["chunk_id"] != chunk["id"]:
        msg = "Reviewer returned a different assigned chunk"
        raise ValueError(msg)
    if item["reviewer_id"] in used_identities:
        msg = "Every chunk attempt requires a fresh reviewer identity"
        raise ValueError(msg)
    used_identities.add(item["reviewer_id"])


def _collect_workers(
    context: _BatchContext,
    state: dict[str, Any],
    wave: dict[str, Any],
    missing: list[dict[str, Any]],
    used_identities: set[str],
) -> list[str]:
    errors = []
    with ThreadPoolExecutor(max_workers=context.manifest["limits"]["max_workers"]) as pool:
        pending = {
            pool.submit(
                context.execute,
                json.dumps(
                    {
                        **context.request,
                        "iteration": wave["iteration"],
                        "review_chunk": {
                            "plan_sha256": wave["plan_sha256"],
                            "wave_id": wave["id"],
                            "chunk": chunk,
                        },
                    },
                    ensure_ascii=False,
                ),
            ): chunk
            for chunk in missing
        }
        for future in as_completed(pending):
            chunk = pending[future]
            event = {"chunk_id": chunk["id"], "attempt_id": uuid.uuid4().hex}
            receipt = None
            try:
                receipt = future.result()
                _accept_worker(context, wave, chunk, receipt, used_identities)
                event["result"] = receipt
            except Exception as exc:
                _LOGGER.exception("Scoped reviewer outcome failed validation")
                if receipt is not None:
                    event["rejected_result"] = receipt
                event["error"] = (
                    "Chunk " + chunk["id"] + ": " + type(exc).__name__ + ": " + str(exc)
                )
                errors.append(event["error"])
            wave["outcomes"].append(event)
            _save(context.root, state)
    return errors


def _coverage_failure(
    manifest: dict[str, Any],
    children: list[dict[str, Any]],
    verify_coverage: Callable[[dict[str, Any], dict[str, Path]], None],
) -> str | None:
    try:
        verify_coverage(
            manifest,
            {child["payload"]["chunk_id"]: ledger_path(child["payload"]) for child in children},
        )
    except Exception as exc:
        _LOGGER.exception("Scoped review aggregate coverage failed validation")
        return type(exc).__name__ + ": " + str(exc)
    return None


def run_batch(  # noqa: PLR0913 -- preserve the trusted callback and keyword-only argument ABI.
    root: Path,
    subject: dict[str, str],
    request: dict[str, Any],
    execute: Callable[[str], dict[str, Any]],
    *,
    discovery_only: bool,
    sign_wave: Callable[[dict[str, Any]], dict[str, Any]],
    verify_coverage: Callable[[dict[str, Any], dict[str, Path]], None],
) -> dict[str, Any]:
    iterations = _iterations()
    manifest = request["review_plan"]
    review_chunks.validate_manifest(subject, manifest)
    plan_sha = review_chunks.plan_digest(manifest)
    context = _BatchContext(root, subject, manifest, request, execute)
    while True:
        state, history, results, wave, missing = _start_wave(
            context, plan_sha, iterations, discovery_only=discovery_only
        )
        errors = _collect_workers(context, state, wave, missing, _used_identities(results, state))
        selected = review_clearances.latest(
            state if discovery_only else review_journal.approval_state(state),
            manifest,
            discovery_only=discovery_only,
            complete=_complete_chunk,
        )
        ordered = [selected[chunk["id"]] for chunk in manifest["chunks"] if chunk["id"] in selected]
        children = [child for child, _ in ordered]
        origins = {child["payload"]["chunk_id"]: origin for child, origin in ordered}
        errors.extend(
            _chunk_diagnostic(child["payload"], origin["iteration"]["recorded_findings"])
            for child, origin in ordered
            if not _complete_chunk(child["payload"], origin["iteration"]["recorded_findings"])
        )
        all_complete = len(children) == len(manifest["chunks"]) and all(
            _complete_chunk(child["payload"], origin["iteration"]["recorded_findings"])
            for child, origin in ordered
        )
        if all_complete:
            failure = _coverage_failure(manifest, children, verify_coverage)
            if failure is not None:
                errors.append(failure)
                all_complete = False
        if not children:
            msg = (
                "All scoped review attempts failed; outcomes retained in "
                + str(_path(root))
                + "\n"
                + "\n".join(errors)
            )
            raise RuntimeError(msg)
        iteration = {
            **wave["iteration"],
            "attempt_id": uuid.uuid4().hex,
            "chunk_iteration": wave["iteration"],
            "previous_rounds_sha256": iterations.findings_digest(results),
            "prior_findings": _prior_findings(iterations, results, subject),
            "child_origins": origins,
        }
        payload = aggregate_payload(subject, iteration, manifest, children, all_complete)
        receipt = sign_wave(payload)
        verify_wave(root, receipt)
        history["attempts"].append(
            {"id": iteration["attempt_id"], "subject": subject, "iteration": iteration}
        )
        iterations._save(root, history)  # noqa: SLF001 - signed aggregate journal persistence.
        iterations.record(root, iteration["attempt_id"], receipt)
        wave["complete"] = all_complete
        _save(root, state)
        if not all_complete or errors:
            msg = (
                "Incomplete parallel review wave; all outcomes retained in "
                + str(_path(root))
                + "\n"
                + "\n".join(errors)
            )
            raise RuntimeError(msg)
        if not _findings(children):
            known = _known(root, subject, state)
            return {
                "status": "needs_fixes" if known else "discovery_complete",
                "discovery_complete": True,
                "approval_eligible": not known and not discovery_only,
                "findings": known,
                "finding_count": len(known),
                "finding_ids": [item["id"] for item in known],
                "rounds": [receipt],
                "recorded_findings": known,
                "retired_chunk_ids": [chunk["id"] for chunk in manifest["chunks"]],
                "retired_chunk_count": len(manifest["chunks"]),
                "scheduled_chunk_ids": wave["scheduled_chunk_ids"],
                "history_path": str(iterations._path(root)),  # noqa: SLF001 - journal diagnostic.
            }


def build_proof(root: Path, waves: list[dict[str, Any]], subject: dict[str, str]) -> dict[str, Any]:
    full = {"version": _WAVE_VERSION, "subject": subject, "waves": waves}
    if _known(root, subject, _read(root)):
        msg = "Current-candidate open findings prevent final approval"
        raise ValueError(msg)
    identities = _validate_full_proof(root, full)
    data = _canonical(full)
    digest = hashlib.sha256(data).hexdigest()
    path = _local().directory(root) / ("proof-" + digest + ".json")
    if path.is_symlink() or (path.exists() and path.read_bytes() != data):
        msg = "Unsafe parallel review proof destination"
        raise ValueError(msg)
    if not path.exists():
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        temporary.replace(path)
    return _compact_summary(full, identities, digest)


def _compact_summary(full: dict[str, Any], identities: list[str], digest: str) -> dict[str, Any]:
    waves = full["waves"]
    manifest = waves[-1]["payload"]["review_wave"]["manifest"]
    return {
        "version": _WAVE_VERSION,
        "proof_sha256": digest,
        "plan_sha256": review_chunks.plan_digest(manifest),
        "inventory_sha256": _digest(manifest["inventory"]),
        "completed_rounds": len(waves),
        "completed_waves": len(waves),
        "chunk_count": len(identities),
        "reviewer_count": len(identities),
        "reviewer_ids_sha256": _digest(identities),
        "wave_sha256s": [_digest(wave) for wave in waves],
    }


def _validate_full_proof(root: Path, proof: dict[str, Any]) -> list[str]:
    iterations = _iterations()
    waves = proof["waves"]
    if proof.get("version") != _WAVE_VERSION or not waves:
        msg = "Current V4 complete scoped clearances are required"
        raise ValueError(msg)
    history = iterations.history(root)
    recorded_waves = [attempt["result"] for attempt in history["attempts"] if "result" in attempt]
    identities = []
    plan = None
    for wave in waves:
        if wave not in recorded_waves:
            msg = "Final approval aggregate is missing from its validated signed history"
            raise ValueError(msg)
        payload = verify_wave(root, wave, complete=True)
        if (
            any(payload[key] != proof["subject"][key] for key in iterations.SUBJECT_KEYS)
            or payload["discovery_only"]
            or payload["verdict"]["findings"]
            or payload["verdict"]["approved"] is not True
            or payload["review_wave"]["iteration"]["recorded_findings"]
            or payload.get("review_procedure") != PROCEDURE
            or payload["review_wave"].get("version") != _WAVE_VERSION
        ):
            msg = "A scoped or discovery-only outcome cannot approve the whole candidate"
            raise ValueError(msg)
        if plan is not None and payload["plan_sha256"] != plan:
            msg = "Review plans changed between clean waves"
            raise ValueError(msg)
        plan = payload["plan_sha256"]
        identities.extend(
            child["payload"]["reviewer_id"] for child in payload["review_wave"]["children"]
        )
    if len(identities) != len(set(identities)):
        msg = "Every scoped clearance must have a distinct fresh reviewer identity"
        raise ValueError(msg)
    final = waves[-1]["payload"]["review_wave"]
    if review_chunks.installed_digest(root, proof["subject"]) != review_chunks.plan_digest(
        final["manifest"]
    ):
        msg = "Final approval does not match the current installed review plan"
        raise ValueError(msg)
    journal = _read(root)
    authenticated = review_journal.approval_state(journal)
    if not authenticated["waves"]:
        msg = "Final approval requires authenticated review journal checkpoints"
        raise ValueError(msg)
    latest = review_clearances.latest(
        authenticated, final["manifest"], discovery_only=False, complete=_complete_chunk
    )
    if any(
        latest.get(child["payload"]["chunk_id"], (None, None))[0] != child
        for child in final["children"]
    ):
        msg = "Final approval requires the latest valid clearance for every assignment"
        raise ValueError(msg)
    return identities


def validate_proof(root: Path, payload: dict[str, Any]) -> None:
    compact = payload.get("review_iterations", {})
    digest = compact.get("proof_sha256", "")
    if (
        not isinstance(digest, str)
        or len(digest) != _SHA256_HEX_LENGTH
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        msg = "A compact parallel review proof is required"
        raise ValueError(msg)
    path = _local().directory(root) / ("proof-" + digest + ".json")
    if (
        path.is_symlink()
        or not path.is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != digest
    ):
        msg = "Parallel review proof is missing, unsafe or modified"
        raise ValueError(msg)
    full = json.loads(path.read_text())
    if any(full["subject"][key] != payload[key] for key in _iterations().SUBJECT_KEYS):
        msg = "Parallel review proof belongs to a different candidate"
        raise ValueError(msg)
    if _known(root, full["subject"], _read(root)):
        msg = "Current-candidate open findings prevent final approval"
        raise ValueError(msg)
    identities = _validate_full_proof(root, full)
    expected = _compact_summary(full, identities, digest)
    if compact != expected:
        msg = "Compact review attestation does not match its full private proof"
        raise ValueError(msg)
    final = full["waves"][-1]["payload"]
    if (
        payload["reviewer_id"] != final["reviewer_id"]
        or payload["execution_sha256"] != final["execution_sha256"]
    ):
        msg = "Final approval does not attest the verified aggregate wave"
        raise ValueError(msg)
