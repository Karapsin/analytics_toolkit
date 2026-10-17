"""Typed source-review progress; incomplete assessment grants no authority."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any
from pathlib import Path

PROTOCOL = 1
CORE = ("head", "base", "task_digest", "producer_id")


def protocol(value: Any) -> bool:
    return type(value) is int and value == PROTOCOL


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def binding(repository: str, task: dict[str, Any]) -> dict[str, Any]:
    if (task.get("kind") != "review" or type(task.get("number")) is not int or task["number"] <= 0
            or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository)
            or any(not isinstance(task.get(name), str) or not task[name] for name in CORE)
            or any(not re.fullmatch(r"[0-9a-f]{40}", task[name]) for name in ("head", "base"))
            or not re.fullmatch(r"[0-9a-f]{64}", task["task_digest"])):
        raise ValueError("Invalid independent source-review binding")
    return {"repository": repository, "number": task["number"], **{name: task[name] for name in CORE}}


@contextmanager
def database(controls: Any) -> Iterator[Any]:
    db = controls.connect()
    try:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS source_reviews (
          repository TEXT, number INTEGER, value TEXT, PRIMARY KEY(repository,number));
        CREATE TABLE IF NOT EXISTS source_review_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, repository TEXT, number INTEGER, value TEXT);
        CREATE TABLE IF NOT EXISTS source_review_completions (
          repository TEXT, number INTEGER, chain TEXT, value TEXT,
          PRIMARY KEY(repository,number,chain));
        """)
        with db:
            yield db
    finally:
        db.close()


def current(controls: Any, repository: str, number: int) -> dict[str, Any] | None:
    with database(controls) as db:
        row = db.execute("SELECT value FROM source_reviews WHERE repository=? AND number=?",
                         (repository, number)).fetchone()
    return json.loads(row[0]) if row else None


def pending(controls: Any, repository: str, number: int, head: str, base: str) -> bool:
    value = current(controls, repository, number)
    return bool(value and value["binding"]["head"] == head and value["binding"]["base"] == base)


def begin(controls: Any, repository: str, task: dict[str, Any], *, previous_sha256: str | None = None,
          transition_id: str | None = None, completed_receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    expected = binding(repository, task)
    if transition_id is not None and not re.fullmatch(r"[0-9a-f]{32}", transition_id):
        raise ValueError("Invalid source-review generation transition identity")
    with database(controls) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM source_reviews WHERE repository=? AND number=?",
                         (repository, task["number"])).fetchone()
        if row is not None:
            previous = json.loads(row[0])
            if previous["binding"] == expected and (transition_id is None or previous.get("transition_id") == transition_id):
                return previous
            if previous_sha256 != digest(previous):
                raise ValueError("Source-review generation changed; revalidate before replacing it")
            db.execute("INSERT INTO source_review_history(repository,number,value) VALUES (?,?,?)",
                       (repository, task["number"], row[0]))
        elif transition_id is not None:
            completion = None
            if completed_receipt is not None:
                completion = db.execute("SELECT value FROM source_review_completions WHERE repository=? AND number=? AND chain=?",
                    (repository, task["number"], completed_receipt.get("review_chain_id"))).fetchone()
            old = json.loads(completion[0]) if completion else None
            history = db.execute("SELECT value FROM source_review_history WHERE repository=? AND number=? LIMIT 1001",
                                 (repository, task["number"])).fetchall()
            if (old is None or old["binding"] != expected
                    or old["terminal"] != {"outcome": "approve", "receipt_sha256": digest(completed_receipt)}
                    or len(history) > 1000
                    or any(json.loads(item[0]).get("transition_id") == transition_id for item in history)):
                raise ValueError("An absent review needs its exact completed approval to supersede pending publication")
        value = {"protocol": PROTOCOL, "binding": expected, "chain_id": "source-review:" + uuid.uuid4().hex,
                 "bundle_sha256": None, "created": time.time()}
        if transition_id is not None:
            value["transition_id"] = transition_id
        if completed_receipt is not None:
            value["supersedes_completion"] = completed_receipt["review_chain_id"]
        db.execute("INSERT OR REPLACE INTO source_reviews VALUES (?,?,?)",
                   (repository, task["number"], json.dumps(value, sort_keys=True)))
    return value


def validate_inventory(inventory: dict[str, Any]) -> None:
    if (not isinstance(inventory, dict)
            or set(inventory) != {"source_paths", "visual_pngs", "required_evidence", "available_evidence"}
            or not inventory["source_paths"] or not inventory["required_evidence"]):
        raise ValueError("A source-review bundle requires its trusted complete inventory")
    for items in inventory.values():
        if (not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items)
                or len(items) != len(set(items))):
            raise ValueError("Invalid source-review coverage inventory")
    if not set(inventory["required_evidence"]) <= set(inventory["available_evidence"]):
        raise ValueError("Required evidence is outside its immutable review bundle")


def bind_bundle(controls: Any, repository: str, task: dict[str, Any], chain_id: str, bundle: str,
                inventory: dict[str, Any]) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", bundle):
        raise ValueError("Source-review bundle digest is missing")
    expected = binding(repository, task)
    validate_inventory(inventory)
    with database(controls) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM source_reviews WHERE repository=? AND number=?",
                         (repository, task["number"])).fetchone()
        if row is None:
            raise ValueError("Independent source review was not begun")
        value = json.loads(row[0])
        if (value["binding"] != expected or value["chain_id"] != chain_id
                or value["bundle_sha256"] not in (None, bundle)):
            raise ValueError("Source-review assignment changed")
        if value.get("inventory") is not None and value["inventory"] != inventory:
            raise ValueError("Source-review coverage inventory changed")
        value["bundle_sha256"] = bundle
        value["inventory"] = inventory
        value.setdefault("required_evidence", list(inventory["required_evidence"]))
        db.execute("UPDATE source_reviews SET value=? WHERE repository=? AND number=? AND value=?",
                   (json.dumps(value, sort_keys=True), repository, task["number"], row[0]))


def require_evidence(controls: Any, repository: str, task: dict[str, Any], chain_id: str,
                     bundle: str, required: list[str]) -> None:
    with database(controls) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM source_reviews WHERE repository=? AND number=?",
                         (repository, task["number"])).fetchone()
        value = json.loads(row[0]) if row else None
        if (value is None or value["binding"] != binding(repository, task)
                or value["chain_id"] != chain_id or value["bundle_sha256"] != bundle
                or any(not isinstance(item, str) for item in required)
                or not set(required) <= set(value["inventory"]["available_evidence"])):
            raise ValueError("Additional review evidence is outside the bound assignment")
        value["required_evidence"] = sorted(set(value["required_evidence"]) | set(required))
        db.execute("UPDATE source_reviews SET value=? WHERE repository=? AND number=? AND value=?",
                   (json.dumps(value, sort_keys=True), repository, task["number"], row[0]))


def verify_assignment(controls: Any, repository: str, task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    value = current(controls, repository, task["number"])
    if (value is None or value["binding"] != binding(repository, task)
            or not protocol(receipt.get("source_review_protocol"))
            or value["chain_id"] != receipt.get("review_chain_id")
            or value["bundle_sha256"] is None
            or value["bundle_sha256"] != receipt.get("review_bundle_sha256")):
        raise ValueError("Independent source-review progress belongs to another assignment")
    return value


def completed(controls: Any, repository: str, task: dict[str, Any], receipt: dict[str, Any], outcome: str) -> bool:
    with database(controls) as db:
        active = db.execute("SELECT value FROM source_reviews WHERE repository=? AND number=?",
                            (repository, task["number"])).fetchone()
        row = db.execute("SELECT value FROM source_review_completions WHERE repository=? AND number=? AND chain=?",
                         (repository, task["number"], receipt.get("review_chain_id"))).fetchone()
    if active or row is None:
        return False
    value = json.loads(row[0])
    if (value["binding"] != binding(repository, task) or value["bundle_sha256"] != receipt.get("review_bundle_sha256")
            or value["terminal"] != {"outcome": outcome, "receipt_sha256": digest(receipt)}):
        raise ValueError("Completed source-review identity changed")
    return True


def finish(controls: Any, repository: str, task: dict[str, Any], receipt: dict[str, Any], outcome: str) -> None:
    if outcome not in {"approve", "reject"}:
        raise ValueError("Only a completed source verdict can retire a pending review")
    if completed(controls, repository, task, receipt, outcome):
        return
    value = verify_assignment(controls, repository, task, receipt)
    encoded = json.dumps(value, sort_keys=True)
    with database(controls) as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute("DELETE FROM source_reviews WHERE repository=? AND number=? AND value=?",
                             (repository, task["number"], encoded)).rowcount
        if changed != 1:
            raise ValueError("Independent source-review progress changed before completion")
        terminal = dict(value, terminal={"outcome": outcome, "receipt_sha256": digest(receipt)})
        db.execute("INSERT INTO source_review_completions VALUES (?,?,?,?)",
                   (repository, task["number"], value["chain_id"], json.dumps(terminal, sort_keys=True)))
        db.execute("INSERT INTO source_review_history(repository,number,value) VALUES (?,?,?)",
                   (repository, task["number"], encoded))


def disposition(verdict: dict[str, Any], receipt: dict[str, Any]) -> str:
    """Legacy or partial results stay pending; only concrete defects request code."""
    if (not protocol(verdict.get("source_review_protocol"))
            or not protocol(receipt.get("source_review_protocol"))):
        return "pending"
    if (verdict.get("completeness") not in {"complete", "incomplete"}
            or not isinstance(verdict.get("confirmed_findings"), list)
            or not isinstance(verdict.get("unread_evidence"), list)
            or any(not isinstance(item, str) or not item.strip() for item in verdict["unread_evidence"])):
        raise ValueError("Malformed source-review completeness report")
    for finding in verdict["confirmed_findings"]:
        if (not isinstance(finding, dict)
                or set(finding) != {"location", "requirement", "failure", "correction"}
                or any(not isinstance(item, str) or not item.strip() for item in finding.values())):
            raise ValueError("Concrete review findings require location, requirement, failure and correction")
    if verdict["confirmed_findings"]:
        if verdict.get("approved") is not False:
            raise ValueError("A concrete defect cannot approve a candidate")
        return "reject"
    if (verdict["completeness"] != "complete" or verdict["unread_evidence"]
            or verdict.get("approved") is not True or verdict.get("alignment") != "aligned"):
        return "pending"
    if (not protocol(receipt.get("source_review_protocol"))
            or receipt.get("coverage_complete") is not True
            or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("coverage_sha256", ""))):
        raise ValueError("Complete source review requires the host-verified coverage receipt")
    return "approve"


def visual_rejection(artifact: Path, repository: str, task: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """Keep actual negative full-scene evidence distinct from partial source work."""
    path = artifact / "visual-rejection.json"
    if not path.exists():
        return False
    manifest_path = artifact.parent / "visual-evidence/manifest.json"
    if path.is_symlink() or manifest_path.is_symlink():
        raise ValueError("Visual rejection evidence cannot be a symbolic link")
    value = json.loads(path.read_text())
    if not protocol(value.get("visual_review_protocol")):
        return False  # A legacy approved=false token cannot prove completed inspection.
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if (value.get("task") != task or value.get("candidate") != task["head"]
            or value.get("capture", {}).get("candidate") != task["head"]
            or value["capture"].get("capture", {}).get("status") != "pass"
            or value["capture"].get("viewport") != [1280, 800]
            or not value.get("findings") or not isinstance(value.get("scenes"), dict)
            or set(value["scenes"]) != set(value["capture"].get("scenes", {}))
            or not any(item.get("verdict") == "product_defect" for item in value["scenes"].values())
            or hashlib.sha256(raw).hexdigest() != receipt.get("visual_evidence_digest")
            or manifest.get("disposition") != "rejected"
            or manifest.get("repository") != repository
            or manifest.get("task") != {key: task[key] for key in ("number", *CORE)}
            or manifest.get("files", {}).get(path.name) != hashlib.sha256(path.read_bytes()).hexdigest()):
        raise ValueError("Visual rejection does not bind this complete current candidate")
    for scene, details in value["scenes"].items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", scene):
            raise ValueError("Invalid visual review scene identity")
        child = artifact.parent / ("scene-" + scene)
        report = json.loads((child / "artifact/verdict.json").read_text())
        assigned = json.loads((child / "scene-binding.json").read_text())
        capture = value["capture"]["scenes"][scene]
        if (not protocol(report.get("visual_review_protocol")) or report.get("inspection_complete") is not True
                or assigned.get("task") != binding(repository, task)
                or assigned.get("scene") != scene or assigned.get("image_sha256") != capture["screenshot_sha256"]
                or assigned.get("geometry_sha256") != capture["geometry_sha256"]
                or details.get("inspection_complete") is not True
                or details.get("confirmed_findings") != report.get("confirmed_findings")
                or not isinstance(report.get("confirmed_findings"), list)
                or any(not isinstance(item, dict) or set(item) != {"region", "failure", "correction"}
                       or any(not isinstance(text, str) or not text.strip() for text in item.values())
                       for item in report["confirmed_findings"])):
            raise ValueError("Visual rejection lacks a complete exact-image child assessment")
        if details["verdict"] == "product_defect":
            if report.get("approved") is not False or not report["confirmed_findings"]:
                raise ValueError("A negative image verdict needs concrete completed findings")
        elif (details["verdict"] != "pass" or report.get("approved") is not True
              or report["confirmed_findings"]):
            raise ValueError("Contradictory visual child verdict")
    return True


def verify_coverage(controls: Any, artifact: Path, repository: str, task: dict[str, Any], receipt: dict[str, Any]) -> None:
    path = artifact / "source-coverage.json"
    if path.is_symlink() or path.stat().st_size > 512 * 1024:
        raise ValueError("Invalid host source-review coverage receipt")
    raw = path.read_bytes()
    value = json.loads(raw)
    assignment = verify_assignment(controls, repository, task, receipt)
    if (hashlib.sha256(raw).hexdigest() != receipt["coverage_sha256"]
            or not protocol(value.get("protocol")) or value.get("binding") != binding(repository, task)
            or value.get("chain_id") != receipt["review_chain_id"]
            or value.get("bundle_sha256") != receipt["review_bundle_sha256"]
            or value.get("complete") is not True or value.get("retrieved_diff_complete") is not True
            or any(not isinstance(value.get(field), str) or not value[field].strip()
                   for field in ("task_assessment", "cross_file_assessment"))):
        raise ValueError("Host source-review coverage does not match this complete assignment")
    for expected, assessed in (("source_paths", "assessed_source_paths"),
                               ("visual_pngs", "assessed_visual_pngs"),
                               ("required_evidence", "assessed_evidence")):
        left, right = value.get(expected), value.get(assessed)
        if (not isinstance(left, list) or not isinstance(right, list)
                or any(not isinstance(item, str) for item in (*left, *right))
                or len(set(left)) != len(left) or len(set(right)) != len(right) or set(left) != set(right)):
            raise ValueError("Host source-review coverage is partial or contradictory")
        required = assignment["required_evidence"] if expected == "required_evidence" else assignment["inventory"][expected]
        if set(left) != set(required):
            raise ValueError("Coverage differs from the trusted immutable bundle inventory")


def blocks_merge(controls: Any, pr: dict[str, Any], base: str) -> bool:
    repository = pr.get("base", {}).get("repo", {}).get("full_name")
    if not repository or not os.environ.get("AGENT_STATE_DIR"):
        return True  # A production merge needs its actual private progress context.
    correction = controls.correction_snapshot(repository, pr["number"])
    return (current(controls, repository, pr["number"]) is not None
            or bool(correction and correction["head"] == pr.get("head", {}).get("sha")))


def terminal_intent(controls: Any, repository: str, task: dict[str, Any], receipt: dict[str, Any],
                    outcome: str, summary: str, reason: str, changes: str) -> dict[str, Any]:
    value = verify_assignment(controls, repository, task, receipt)
    desired = {"receipt_sha256": digest(receipt), "outcome": outcome, "summary": summary,
               "reason": reason, "changes": changes}
    with database(controls) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM source_reviews WHERE repository=? AND number=?",
                         (repository, task["number"])).fetchone()
        if row is None or json.loads(row[0]) != value:
            raise ValueError("Terminal source-review generation changed")
        existing = value.get("terminal_intent")
        if existing:
            if {key: existing[key] for key in desired} != desired:
                raise ValueError("Terminal source-review decision changed during recovery")
            return existing
        correction = db.execute("SELECT * FROM corrections WHERE repository=? AND number=?",
                                (repository, task["number"])).fetchone()
        baseline = dict(correction) if correction else None
        if baseline and baseline["head"] == task["head"] and baseline["digest"] == task["task_digest"]:
            raise ValueError("A newer concrete correction already owns this candidate")
        intent = dict(desired, previous_correction=baseline)
        value["terminal_intent"] = intent
        db.execute("UPDATE source_reviews SET value=? WHERE repository=? AND number=? AND value=?",
                   (json.dumps(value, sort_keys=True), repository, task["number"], row[0]))
        return intent


def publish_terminal(controls: Any, gh: Any, task: dict[str, Any], receipt: dict[str, Any],
                     outcome: str, summary: str, reason: str, changes: str, authority: Any) -> None:
    """Complete each owned side effect, never replay rejection over later facts."""
    app_id = receipt.get("publisher_app_id")
    if type(app_id) is not int or app_id <= 0:
        raise ValueError("Typed publication needs the trusted host publisher identity")
    marker = f"<!-- agent-source-review-{receipt['review_chain_id']}-{digest(receipt)}-{outcome} -->"
    summary = marker + "\n" + summary
    if len(summary) > 60000 or len(reason) > 60000:
        raise ValueError("Terminal review must retain complete findings within publication capacity")
    intent = terminal_intent(controls, gh.repository, task, receipt, outcome, summary, reason, changes)

    def own_correction() -> dict[str, Any] | None:
        value = controls.correction_snapshot(gh.repository, task["number"])
        if outcome == "reject" and value and all(value.get(key) == expected for key, expected in
                (("head", task["head"]), ("digest", task["task_digest"]), ("reason", reason), ("changes", changes))):
            return value
        if value != intent["previous_correction"]:
            raise ValueError("Newer correction findings cannot be overwritten by source-review recovery")
        return value

    pr = authority()
    own_correction()
    selected = next((item for item in gh.checks(task["head"]) if item.get("name") == "agent-review"
                     and item.get("app", {}).get("id") == app_id and item.get("external_id") == task["base"]), None)
    expected_conclusion = "success" if outcome == "approve" else "failure"
    if not (selected and selected.get("status") == "completed" and selected.get("conclusion") == expected_conclusion
            and selected.get("output", {}).get("summary") == summary):
        # A matching owned pending check may advance to terminal. A competing
        # terminal result is a coordinator conflict, never permission to replace it.
        if selected and selected.get("status") == "completed":
            raise ValueError("Another terminal reviewer owns the current public check")
        gh.api("check-runs", "POST", {"name": "agent-review", "head_sha": task["head"],
            "external_id": task["base"], "status": "completed", "conclusion": expected_conclusion,
            "output": {"title": "Approved" if outcome == "approve" else "Changes requested", "summary": summary}})
    authority()
    own_correction()
    reviews = gh.pages(f"pulls/{task['number']}/reviews?per_page=100")
    if not any(item.get("commit_id") == task["head"] and item.get("body") == summary
               and item.get("user", {}).get("type") == "Bot" for item in reviews):
        gh.api(f"pulls/{task['number']}/reviews", "POST", {"commit_id": task["head"], "event": "COMMENT", "body": summary})
    if outcome == "reject":
        before = own_correction()
        if before == intent["previous_correction"]:
            if not controls.require_correction(gh.repository, task["number"], task["head"], task["task_digest"],
                                                reason, changes, expected=before):
                raise ValueError("Correction authority changed during source-review publication")
        own_correction()
        comment_marker = f"<!-- agent-correction-{task['head']}-{task['task_digest']} -->"
        if not any(comment_marker in item.get("body", "") and item.get("user", {}).get("type") == "Bot"
                   for item in gh.pages(f"issues/{task['number']}/comments?per_page=100")):
            gh.comment(task["number"], comment_marker + "\nReviewer requested changes.\n\nWhat changed: " + changes
                       + "\n\nWhy rejected:\n" + reason + "\n\nA separate correction agent on RPi5 is queued. "
                       "It will plan the correction and submit it for independent review.")
        controls.enqueue(f"reject:{gh.repository}:{task['number']}:{task['head']}:{task['task_digest']}", "rejection",
            {"repository": gh.repository, "number": task["number"], "head": task["head"],
             "url": pr.get("html_url", f"https://github.com/{gh.repository}/pull/{task['number']}"),
             "title": pr.get("title", "PR " + str(task["number"])), "changes": changes, "reason": reason,
             "command": "A separate RPi5 correction agent is queued; no resume command is needed.",
             **controls._notification_metadata(pr, "review_rejection", task["task_digest"])})
    authority()
    own_correction()
    finish(controls, gh.repository, task, receipt, outcome)
