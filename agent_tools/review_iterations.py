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

MINIMUM_ROUNDS = 1
PROCEDURE = "discovery-chunks-v4"
PARALLEL_PROCEDURES = {"discovery-chunks-v3", PROCEDURE}
SUBJECT_KEYS = ("repository", "head", "base", "tree", "task_digest", "producer_id")
INSTRUCTIONS = (
    "Review your complete ASSIGNED component or integration scope and relevant interacting source. "
    "This bounded parallel assignment supersedes any older prefix asking each worker to read "
    "the whole candidate. Do not blanket-scan every diff; read EVERY assigned diff page. "
    "No individual chunk can approve the whole feature; the coordinator aggregates ALL chunks. "
    "A completed zero-new scope retires for this frozen candidate and assignment; only scopes "
    "with new issues or incomplete results are scheduled again. Exclude only the "
    "supplied OPEN finding IDs for this exact frozen candidate; alternative wording or examples "
    "of the same current issue are not novel. A new candidate starts with EMPTY finding history. "
    "All assigned pages, interacting source and relevant current task obligations remain required. "
    "Continue through the complete assigned immutable scope in bounded groups, semantically "
    "assessing each assigned diff page and relevant interacting source; recover truncated reads. "
    "Before a final no-findings verdict, complete that inspection rather than stopping merely "
    "after a small initial sample or a size estimate while work can continue. Collect ALL novel "
    "issues without stopping for repair. The candidate stays frozen between discovery rounds. "
    "Exclude current OPEN IDs and semantic duplicates from novel findings. "
    "Findings contains only novel [P0..P3] issues. Current open issues require approved=false "
    "and alignment=not_aligned; complete zero-novel discovery closes the batch for repair, "
    "never approval. A changed candidate restarts full discovery without historical findings. "
    "Actual tool failures, unresolved uncertainty or genuinely "
    "incomplete inspection still require rejection; never pressure approval or relax coverage. "
    "Put every additional concrete defect in findings, prefixed [P0], [P1], [P2] or [P3]. "
    "Describe incomplete inspection in summary; never approve it. Only return approved=true, "
    "alignment=aligned and findings=[] only for a completely inspected assigned scope with "
    "no open issues; this is never whole-feature approval. There is no retry cap."
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
    previous: list[dict[str, Any]] = []
    for attempt in value["attempts"]:
        if "result" in attempt:
            payload = verify_round(root, attempt["result"])
            if any(payload[key] != attempt["subject"][key] for key in SUBJECT_KEYS) or payload[
                "previous_rounds_sha256"
            ] != findings_digest(previous):
                msg = "Independent review history does not follow its signed previous chain"
                raise ValueError(msg)
            iteration = attempt["iteration"]
            expected_findings = (
                _findings(previous, attempt["subject"])
                if iteration.get("procedure") in PARALLEL_PROCEDURES
                else iteration.get("prior_findings", _findings(previous))
            )
            if payload["prior_findings_sha256"] != findings_digest(expected_findings):
                msg = "Independent review history changed its signed previous findings"
                raise ValueError(msg)
            previous.append(attempt["result"])
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
        or (
            p.get("review_procedure") is not None
            and (
                p["review_procedure"]
                not in {"discovery-batch-v1", "discovery-batch-v2", *PARALLEL_PROCEDURES}
                or not isinstance(p.get("discovery_only"), bool)
                or not re.fullmatch(r"[0-9a-f]{64}", str(p.get("recorded_findings_sha256", "")))
            )
        )
    ):
        msg = "Invalid independent review round provenance"
        raise ValueError(msg)
    if p.get("review_procedure") in PARALLEL_PROCEDURES:
        if (
            p.get("review_scope") not in {"chunk", "wave"}
            or not re.fullmatch(r"[0-9a-f]{64}", str(p.get("plan_sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{32}", str(p.get("wave_id", "")))
        ):
            msg = "Parallel review scope provenance is required"
            raise ValueError(msg)
        if p["review_scope"] == "wave":
            from agent_tools import review_waves  # noqa: PLC0415 - aggregate provenance validation.

            review_waves.verify_wave(root, receipt)
    return p


def _scope(payload: dict[str, Any], subject: dict[str, str]) -> bool:
    return all(payload[k] == subject[k] for k in ("repository", "task_digest", "base"))


def _same_candidate(payload: dict[str, Any], subject: dict[str, Any]) -> bool:
    return all(payload[key] == subject[key] for key in SUBJECT_KEYS if key != "task_digest")


def _findings(
    results: list[dict[str, Any]], subject: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "subject": {k: r["payload"][k] for k in SUBJECT_KEYS},
            "reviewer_id": r["payload"]["reviewer_id"],
            "findings": r["payload"]["verdict"].get("findings", []),
        }
        for r in results
        if r["payload"]["verdict"].get("findings")
        and (subject is None or _same_candidate(r["payload"], subject))
    ]


def findings_digest(findings: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(findings, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def recorded_findings(
    results: list[dict[str, Any]], subject: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Assign stable IDs without rewriting any signed historical verdict."""
    known: dict[str, dict[str, Any]] = {}
    for result in results:
        payload = result["payload"]
        if subject is not None and not _same_candidate(payload, subject):
            continue
        for finding in payload["verdict"].get("findings", []):
            if not isinstance(finding, str) or not re.match(r"^\[P[0-3]\]\s", finding):
                continue
            identity = "finding:" + hashlib.sha256(finding.encode("utf-8")).hexdigest()
            item = known.setdefault(identity, {"id": identity, "finding": finding, "origins": []})
            origin = {
                "subject": {key: payload[key] for key in SUBJECT_KEYS},
                "reviewer_id": payload["reviewer_id"],
            }
            if origin not in item["origins"]:
                item["origins"].append(origin)
    return list(known.values())


def _current_findings(known: list[dict[str, Any]], subject: dict[str, Any]) -> list[dict[str, Any]]:
    keys = tuple(key for key in SUBJECT_KEYS if key != "task_digest")
    return [
        item
        for item in known
        if any(
            all(origin["subject"][key] == subject[key] for key in keys)
            for origin in item["origins"]
        )
    ]


def _complete_discovery(payload: dict[str, Any], known: list[dict[str, Any]]) -> bool:
    verdict = payload.get("verdict", {})
    if (
        not isinstance(verdict, dict)
        or not isinstance(verdict.get("approved"), bool)
        or not isinstance(verdict.get("summary"), str)
        or not verdict["summary"].strip()
        or not isinstance(verdict.get("change_summary"), str)
        or not isinstance(verdict.get("findings"), list)
        or not all(isinstance(finding, str) for finding in verdict["findings"])
        or payload.get("coverage_complete") is not True
        or verdict.get("alignment") not in {"aligned", "not_aligned"}
        or not all(re.match(r"^\[P[0-3]\]\s", finding) for finding in verdict.get("findings", []))
    ):
        return False
    if any(
        "finding:" + hashlib.sha256(finding.encode("utf-8")).hexdigest()
        in {item["id"] for item in known}
        for finding in verdict["findings"]
    ):
        return False
    if verdict["findings"] or known:
        return verdict.get("approved") is False and verdict.get("alignment") == "not_aligned"
    return _local()._complete_verdict(verdict)  # noqa: SLF001 - shared verdict contract.


class DiscoveryBatchError(RuntimeError):
    """A complete discovery batch requires aggregate author repair, not approval."""

    def __init__(self, batch: dict[str, Any]) -> None:
        self.batch = batch
        findings = "\n".join(item["id"] + " " + item["finding"] for item in batch["findings"])
        super().__init__(
            "Local review rejected; discovery batch complete and requires fixes: "
            + str(batch["finding_count"])
            + " issues; full signed history: "
            + batch["history_path"]
            + "\n"
            + findings
        )


def record(root: Path, attempt_id: str, receipt: dict[str, Any]) -> None:
    payload = verify_round(root, receipt)
    state = history(root)
    attempt = next(x for x in state["attempts"] if x["id"] == attempt_id)
    if any(payload[k] != attempt["subject"][k] for k in SUBJECT_KEYS):
        msg = "Review round changed its candidate binding"
        raise ValueError(msg)
    other = [x["result"] for x in state["attempts"] if x["id"] != attempt_id and "result" in x]
    if any(
        x["payload"]["reviewer_id"] == payload["reviewer_id"]
        and not (
            payload.get("review_scope") == "wave"
            and x["payload"].get("review_scope") == "wave"
            and (
                payload.get("review_procedure") == PROCEDURE
                or (
                    payload.get("wave_id") == x["payload"].get("wave_id")
                    and payload.get("plan_sha256") == x["payload"].get("plan_sha256")
                )
            )
        )
        for x in other
    ):
        msg = "Every independent review iteration requires a fresh reviewer identity"
        raise ValueError(msg)
    if payload["previous_rounds_sha256"] != findings_digest(other):
        msg = "Review iteration does not follow the actual previous round history"
        raise ValueError(msg)
    iteration = attempt["iteration"]
    active_subject = (
        attempt["subject"]
        if iteration.get("procedure") in {"discovery-batch-v2", *PARALLEL_PROCEDURES}
        else None
    )
    if payload["prior_findings_sha256"] != findings_digest(_findings(other, active_subject)):
        msg = "Review round does not bind the actual previous findings"
        raise ValueError(msg)
    if iteration.get("procedure") == PROCEDURE and (
        payload.get("review_procedure") != PROCEDURE
        or payload.get("discovery_only") is not iteration["discovery_only"]
        or payload.get("recorded_findings_sha256")
        != findings_digest(iteration["recorded_findings"])
    ):
        msg = "Review round does not bind its discovery procedure and known findings"
        raise ValueError(msg)
    if "result" in attempt and attempt["result"] != receipt:
        msg = "Independent review outcome is immutable"
        raise ValueError(msg)
    attempt["result"] = receipt
    _save(root, state)


def run(  # noqa: PLR0913 - keep trusted execution, signing and coverage callbacks explicit.
    root: Path,
    subject: dict[str, str],
    request: dict[str, Any],
    execute: Callable[[str], dict[str, Any]],
    *,
    sign_wave: Callable[[dict[str, Any]], dict[str, Any]],
    verify_wave: Callable[[dict[str, Any], dict[str, Path]], None],
) -> list[dict[str, Any]]:
    """Complete discovery before either aggregate repair or exact-candidate approval."""
    from agent_tools import review_waves  # noqa: PLC0415 - scoped wave orchestration.

    batch = review_waves.run_batch(
        root,
        subject,
        request,
        execute,
        discovery_only=False,
        sign_wave=sign_wave,
        verify_coverage=verify_wave,
    )
    if batch["status"] == "needs_fixes":
        raise DiscoveryBatchError(batch)
    return cast("list[dict[str, Any]]", batch["rounds"])


def discover(  # noqa: PLR0913 - match the approval orchestrator's explicit trusted callbacks.
    root: Path,
    subject: dict[str, str],
    request: dict[str, Any],
    execute: Callable[[str], dict[str, Any]],
    *,
    sign_wave: Callable[[dict[str, Any]], dict[str, Any]],
    verify_wave: Callable[[dict[str, Any], dict[str, Path]], None],
) -> dict[str, Any]:
    """Inspect immutable source without issuing or reusing a publication approval."""
    from agent_tools import review_waves  # noqa: PLC0415 - scoped wave orchestration.

    return review_waves.run_batch(
        root,
        subject,
        request,
        execute,
        discovery_only=True,
        sign_wave=sign_wave,
        verify_coverage=verify_wave,
    )


def compact_proof(
    root: Path, waves: list[dict[str, Any]], subject: dict[str, str]
) -> dict[str, Any]:
    from agent_tools import review_waves  # noqa: PLC0415 - private proof retention.

    return review_waves.build_proof(root, waves, subject)


def validate_convergence(root: Path, payload: dict[str, Any]) -> None:
    from agent_tools import review_waves  # noqa: PLC0415 - signed aggregate proof validation.

    review_waves.validate_proof(root, payload)
