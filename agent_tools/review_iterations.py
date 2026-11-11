"""Durable fresh-review rounds; findings transfer, approvals do not."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import TYPE_CHECKING, Any, Callable, cast

if TYPE_CHECKING:
    from pathlib import Path

from agent_tools import task_context

MINIMUM_ROUNDS = 2
SUBJECT_KEYS = ("repository", "head", "base", "tree", "task_digest", "producer_id")
INSTRUCTIONS = (
    "Start a fresh independent review of the complete current source and original task. "
    "For iteration 2 and later, explicitly search for issues NOT found in earlier iterations. "
    "Independently verify every previously reported issue is resolved; repeated unresolved issues "
    "still block approval. Prior findings are evidence to investigate, not prior approval or "
    "transferred inspection. All source pages, interactions and task obligations remain required. "
    "Put every unresolved concrete defect in findings, prefixed [P0], [P1], [P2] or [P3]. "
    "Describe incomplete inspection in summary; never approve it. Only return approved=true, "
    "alignment=aligned and findings=[] after complete inspection finds no new issues and verifies "
    "all prior issues resolved. There is no retry cap."
)


def _local() -> Any:
    from agent_tools import local_review  # noqa: PLC0415 - reviewer/journal dependency cycle.

    return local_review


def _path(root: Path) -> Path:
    return cast(
        "Path",
        _local().directory(root) / ("iterations-" + task_context.read(root)["task_id"] + ".json"),
    )


def _save(root: Path, value: dict[str, Any]) -> None:
    target = _path(root)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes(_local().protocol(root).canonical(value))
    temporary.chmod(0o600)
    temporary.replace(target)


def history(root: Path) -> dict[str, Any]:
    path = _path(root)
    if not path.exists():
        return {"version": 1, "attempts": []}
    if path.is_symlink():
        msg = "Unsafe independent review history"
        raise ValueError(msg)
    value: dict[str, Any] = json.loads(path.read_text())
    if value.get("version") != 1 or not isinstance(value.get("attempts"), list):
        msg = "Invalid independent review history"
        raise ValueError(msg)
    for attempt in value["attempts"]:
        if "result" in attempt:
            verify_round(root, attempt["result"])
    return value


def classification(payload: dict[str, Any]) -> str:
    verdict = payload["verdict"]
    if (
        not isinstance(verdict, dict)
        or type(verdict.get("approved")) is not bool
        or verdict.get("alignment") not in {"aligned", "not_aligned", "uncertain"}
        or not isinstance(verdict.get("summary"), str)
        or not verdict["summary"].strip()
        or not isinstance(verdict.get("change_summary"), str)
        or not isinstance(verdict.get("findings"), list)
        or not all(isinstance(x, str) for x in verdict["findings"])
    ):
        return "incomplete"
    if (
        _local()._complete_verdict(verdict)  # noqa: SLF001 - shared verdict contract.
        and not verdict["findings"]
        and payload["coverage_complete"] is True
    ):
        return "clean"
    if verdict.get("alignment") == "not_aligned" and any(
        isinstance(x, str) and re.match(r"^\[P[0-3]\]\s", x) for x in verdict["findings"]
    ):
        return "issues"
    return "incomplete"


def verify_round(root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    _local().verify_signature(root, receipt)
    p: dict[str, Any] = receipt["payload"]
    if (
        p.get("version") != 1
        or p.get("kind") != "local-review-round"
        or not all(isinstance(p.get(k), str) and p[k] for k in SUBJECT_KEYS)
        or not re.fullmatch(r"reviewer:[0-9a-f-]{36}", str(p.get("reviewer_id", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(p.get("execution_sha256", "")))
        or not isinstance(p.get("verdict"), dict)
        or not isinstance(p.get("coverage_complete"), bool)
        or not re.fullmatch(r"[0-9a-f]{64}", str(p.get("prior_findings_sha256", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(p.get("previous_rounds_sha256", "")))
    ):
        msg = "Invalid independent review round provenance"
        raise ValueError(msg)
    return p


def _scope(payload: dict[str, Any], subject: dict[str, str]) -> bool:
    return all(payload[k] == subject[k] for k in ("repository", "task_digest", "base"))


def _findings(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "subject": {k: r["payload"][k] for k in SUBJECT_KEYS},
            "reviewer_id": r["payload"]["reviewer_id"],
            "findings": r["payload"]["verdict"].get("findings", []),
        }
        for r in results
        if r["payload"]["verdict"].get("findings")
    ]


def findings_digest(findings: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(findings, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def record(root: Path, attempt_id: str, receipt: dict[str, Any]) -> None:
    payload = verify_round(root, receipt)
    state = history(root)
    attempt = next(x for x in state["attempts"] if x["id"] == attempt_id)
    if any(payload[k] != attempt["subject"][k] for k in SUBJECT_KEYS):
        msg = "Review round changed its candidate binding"
        raise ValueError(msg)
    other = [x["result"] for x in state["attempts"] if x["id"] != attempt_id and "result" in x]
    if any(x["payload"]["reviewer_id"] == payload["reviewer_id"] for x in other):
        msg = "Every independent review iteration requires a fresh reviewer identity"
        raise ValueError(msg)
    if payload["previous_rounds_sha256"] != findings_digest(other):
        msg = "Review iteration does not follow the actual previous round history"
        raise ValueError(msg)
    if payload["prior_findings_sha256"] != findings_digest(_findings(other)):
        msg = "Review round does not bind the actual previous findings"
        raise ValueError(msg)
    if "result" in attempt and attempt["result"] != receipt:
        msg = "Independent review outcome is immutable"
        raise ValueError(msg)
    attempt["result"] = receipt
    _save(root, state)


def run(
    root: Path,
    subject: dict[str, str],
    request: dict[str, Any],
    execute: Callable[[str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resume the next pending iteration; only a complete current round can converge."""
    while True:
        state = history(root)
        results = [x["result"] for x in state["attempts"] if "result" in x]
        usable = [
            r
            for r in results
            if _scope(r["payload"], subject) and classification(r["payload"]) != "incomplete"
        ]
        if usable and len(usable) >= MINIMUM_ROUNDS:
            final = usable[-1]["payload"]
            if (
                all(final[k] == subject[k] for k in SUBJECT_KEYS)
                and classification(final) == "clean"
            ):
                return usable
        attempt_id = uuid.uuid4().hex
        iteration = {
            "attempt_id": attempt_id,
            "number": len(usable) + 1,
            "minimum_completed_rounds": MINIMUM_ROUNDS,
            "instructions": INSTRUCTIONS,
            "prior_findings": _findings(results),
            "previous_rounds_sha256": findings_digest(results),
        }
        state["attempts"].append({"id": attempt_id, "subject": subject, "iteration": iteration})
        _save(root, state)
        receipt = execute(json.dumps({**request, "iteration": iteration}, ensure_ascii=False))
        record(root, attempt_id, receipt)
        outcome = classification(receipt["payload"])
        if outcome == "issues":
            verdict = receipt["payload"]["verdict"]
            concrete = [x for x in verdict["findings"] if re.match(r"^\[P[0-3]\]\s", x)]
            msg = (
                "Local review rejected; concrete source issues require correction: "
                + "\n".join(x[:1200] for x in concrete[:2])
                + "\nReviewer summary: "
                + verdict["summary"][:500]
            )
            raise RuntimeError(msg)
        if outcome != "clean":
            msg = (
                "Local review rejected; incomplete inspection or complete diff coverage missing: "
                + str(receipt["payload"]["verdict"].get("summary", outcome))
            )
            raise RuntimeError(msg)


def validate_convergence(root: Path, payload: dict[str, Any]) -> None:
    proof = payload.get("review_iterations")
    if not isinstance(proof, dict) or proof.get("version") != 1:
        msg = "Converged independent review iterations are required before publication"
        raise ValueError(msg)
    rounds = proof.get("rounds")
    if not isinstance(rounds, list) or len(rounds) < MINIMUM_ROUNDS:
        msg = "At least two completed independent review iterations are required"
        raise ValueError(msg)
    identities = set()
    for receipt in rounds:
        p = verify_round(root, receipt)
        if (
            not _scope(p, payload)
            or classification(p) == "incomplete"
            or p["reviewer_id"] in identities
        ):
            msg = "Independent review iteration proof is incomplete or inconsistent"
            raise ValueError(msg)
        identities.add(p["reviewer_id"])
    final = rounds[-1]["payload"]
    if (
        any(final[k] != payload[k] for k in SUBJECT_KEYS)
        or classification(final) != "clean"
        or payload["reviewer_id"] != final["reviewer_id"]
        or payload["execution_sha256"] != final["execution_sha256"]
    ):
        msg = "Final independent review does not cleanly approve this exact candidate"
        raise ValueError(msg)
