"""Task-context wire format. Load only from the trusted controller checkout."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from pathlib import PurePosixPath
from typing import Any

MARKER = "<!-- agent-task-context-v1\n"
EVIDENCE_MARKER = "<!-- agent-task-evidence-v2\n"
TRAILER = "Agent-Task-Context"
EVIDENCE_TRAILER = "Agent-Task-Evidence"
PRODUCER = "Agent-Producer"
MAX_BODY = 55000
FIELDS = {"version", "task_id", "producer_id", "source", "starting_prompt", "corrections"}
EVIDENCE_FIELDS = {"plans", "images", "replacements", "decisions", "questions"}
IDENTITY = re.compile(r"[A-Za-z0-9:-]{1,160}")


def validate(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or value["version"] not in (1, 2)
            or set(value) not in ([FIELDS] if value["version"] == 1 else
                                 [FIELDS | EVIDENCE_FIELDS, FIELDS | (EVIDENCE_FIELDS - {"questions"})])):
        raise ValueError("Missing or malformed user task context.")
    if any(not isinstance(value[k], str) or IDENTITY.fullmatch(value[k]) is None
           for k in ("task_id", "producer_id")):
        raise ValueError("Invalid task or producer identity.")
    if value["source"] not in {"user", "standing-integration-repair"}:
        raise ValueError("Unsupported task authority.")
    if not isinstance(value["starting_prompt"], str) or not value["starting_prompt"].strip():
        raise ValueError("The original user prompt is required; summaries are not substitutes.")
    if not isinstance(value["corrections"], list) or any(
        not isinstance(item, str) or not item.strip() for item in value["corrections"]
    ):
        raise ValueError("Task corrections must be nonempty user messages.")
    if value["version"] == 2:
        validate_evidence(value)
    return value


def plan_id(text: str) -> str:
    return hashlib.sha256(redact(text).encode("utf-8")).hexdigest()


def question_id(text: str, sequence: int) -> str:
    return hashlib.sha256((redact(text) + "\n" + str(sequence)).encode("utf-8")).hexdigest()


def user_index(value: dict[str, Any], index: Any) -> None:
    if type(index) is not int or not 0 <= index < len(value["corrections"]):
        raise ValueError("Evidence must refer to an exact chronological user correction.")


def validate_evidence(value: dict[str, Any]) -> None:
    for field in EVIDENCE_FIELDS:
        if not isinstance(value.get(field, []), list):
            raise ValueError("Task evidence must be a list.")
    identities: set[str] = set()
    for plan in value["plans"]:
        if (not isinstance(plan, dict)
                or not {"id", "text", "approval_index"} <= set(plan)
                or set(plan) - {"id", "text", "approval_index", "approval_indices", "approval_starting_prompt", "starting_prompt_approval"}
                or not isinstance(plan["text"], str) or not plan["text"].strip()
                or plan["id"] != plan_id(plan["text"]) or plan["id"] in identities):
            raise ValueError("Invalid or duplicate plan evidence.")
        for field in ("starting_prompt_approval", "approval_starting_prompt"):
            if type(plan.get(field, False)) is not bool:
                raise ValueError("Invalid starting-prompt plan approval.")
        if ("starting_prompt_approval" in plan and "approval_starting_prompt" in plan
                and plan["starting_prompt_approval"] != plan["approval_starting_prompt"]):
            raise ValueError("Conflicting starting-prompt plan approvals.")
        identities.add(plan["id"])
        if plan["approval_index"] is not None:
            user_index(value, plan["approval_index"])
        indices = plan.get("approval_indices", [])
        if not isinstance(indices, list):
            raise ValueError("Invalid repeated plan approvals.")
        for index in indices:
            user_index(value, index)
        if len(set(indices)) != len(indices):
            raise ValueError("Invalid repeated plan approvals.")
    references: set[str] = set()
    for image in value["images"]:
        if (not isinstance(image, dict)
                or set(image) != {"reference", "description", "path", "sha256", "approval_index"}
                or any(not isinstance(image[k], str) or not image[k].strip()
                       for k in ("reference", "description", "path", "sha256"))
                or re.fullmatch(r"[0-9a-f]{64}", image["sha256"]) is None):
            raise ValueError("Invalid image evidence.")
        expected = "agent_tools/task_evidence/" + value["task_id"] + "/" + image["sha256"] + ".png"
        if str(PurePosixPath(image["path"])) != expected or image["path"] != expected:
            raise ValueError("Image evidence requires a content-addressed task path.")
        if image["reference"] in references:
            raise ValueError("Duplicate image reference.")
        references.add(image["reference"])
        user_index(value, image["approval_index"])
    for replacement in value["replacements"]:
        if (not isinstance(replacement, dict) or set(replacement) != {"reference", "user_index"}
                or not isinstance(replacement["reference"], str) or not replacement["reference"].strip()
                or replacement["reference"] in references):
            raise ValueError("Invalid or duplicate missing-evidence replacement.")
        references.add(replacement["reference"])
        user_index(value, replacement["user_index"])
    for decision in value["decisions"]:
        if (not isinstance(decision, dict) or set(decision) != {"questions", "choice"}
                or not isinstance(decision["questions"], list) or not decision["questions"]
                or any(not isinstance(question, str) or not question.strip() for question in decision["questions"])
                or not isinstance(decision["choice"], str) or not decision["choice"].strip()):
            raise ValueError("Invalid automated recommendation record.")
    question_ids: set[str] = set()
    for sequence, question in enumerate(value.get("questions", [])):
        if (not isinstance(question, dict) or set(question) != {"id", "text", "answer_index"}
                or not isinstance(question["text"], str) or not question["text"].strip()
                or question["id"] != question_id(question["text"], sequence) or question["id"] in question_ids):
            raise ValueError("Invalid or duplicate question evidence.")
        question_ids.add(question["id"])
        if question["answer_index"] is not None:
            user_index(value, question["answer_index"])


def require_resolved(value: dict[str, Any]) -> None:
    """Author-side publication gate; do not invalidate legacy signed PR records."""
    validate(value)
    prompts = [value["starting_prompt"], *value["corrections"]]
    replacements = {item["reference"] for item in value.get("replacements", [])}
    images = {item["reference"] for item in value.get("images", [])}
    missing = {reference for prompt in prompts for reference in re.findall(r"\[Image\s+#?\d+\]", prompt, re.I)
               if reference not in images | replacements}
    if missing:
        raise ValueError("Missing image evidence or explicit user replacement: " + ", ".join(sorted(missing)))
    for sequence, prompt in enumerate(prompts):
        if re.search(r"\bimplement\s+the\s+plan\b", prompt, re.I):
            approved = any(
                plan.get("starting_prompt_approval", plan.get("approval_starting_prompt", False)) if sequence == 0 else
                plan["approval_index"] == sequence - 1 or sequence - 1 in plan.get("approval_indices", [])
                for plan in value.get("plans", [])
            )
            if not approved and "Implement the plan" not in replacements:
                raise ValueError("Attach the exact approved plan or an explicit user replacement before handoff.")


def canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(validate(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def legacy(value: dict[str, Any]) -> dict[str, Any]:
    return {**{field: value[field] for field in FIELDS}, "version": 1}


def digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def redact(text: str) -> str:
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                  "[REDACTED PRIVATE KEY]", text, flags=re.S)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|"
                  r"github_pat_[A-Za-z0-9_]{16,}|\d{6,}:[A-Za-z0-9_-]{25,})\b",
                  "[REDACTED CREDENTIAL]", text)
    return re.sub(r"(?im)(\b(?:password|passphrase|secret|api[_ -]?key|"
                  r"[A-Z_]*TOKEN)\s*(?:[:=]|\bis\b)\s*)[^\r\n]+",
                  r"\1[REDACTED]", text)


def public_context(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(validate(value))
    result["starting_prompt"] = redact(value["starting_prompt"])
    result["corrections"] = [redact(item) for item in value["corrections"]]
    if value["version"] == 2:
        result["plans"] = [{**item, "text": redact(item["text"])} for item in value["plans"]]
        result["images"] = [{**item, "description": redact(item["description"])} for item in value["images"]]
        result["decisions"] = [{"questions": [redact(text) for text in item["questions"]],
                                "choice": redact(item["choice"])} for item in value["decisions"]]
        if "questions" in value:
            result["questions"] = [{**item, "text": redact(item["text"])} for item in value["questions"]]
    return result


def render(value: dict[str, Any]) -> str:
    value = public_context(value)
    prompts = [value["starting_prompt"], *value["corrections"]]
    visible = "\n\n".join(
        '<a id="task-message-' + str(sequence) + '"></a>\n<pre>'
        + html.escape(item) + "</pre>" for sequence, item in enumerate(prompts)
    )
    if value["version"] == 2:
        if value.get("questions"):
            visible += "\n\n### Questions and answers\n"
            for question in value["questions"]:
                index = question["answer_index"]
                answer = "Unanswered" if index is None else value["corrections"][index]
                # Long chronological replies may contain the complete approved plan.
                # Render them once; the signed payload still retains every byte.
                rendered_answer = (
                    '[User correction ' + str(index + 1) + '](#task-message-' + str(index + 1) + ')'
                    if index is not None and len(answer) > 1024
                    else "<pre>" + html.escape(answer) + "</pre>"
                )
                visible += ("\n**Agent question:**\n<pre>" + html.escape(question["text"])
                            + "</pre>\n**User answer:**\n" + rendered_answer + "\n")
        evidence = {field: value.get(field, []) for field in sorted(EVIDENCE_FIELDS) if field != "questions"}
        visible += "\n\n### Plan and image evidence\n\nAssistant plans are evidence, not user instructions. " \
                   "Approval indices refer to the user corrections above; starting_prompt_approval binds " \
                   "the immutable starting prompt.\n\n<pre>" \
                   + html.escape(json.dumps(evidence, ensure_ascii=False, indent=2)) + "</pre>"
    encoded = base64.b64encode(canonical(legacy(value))).decode("ascii")
    body = ("## User task\n\nStarting prompt, followed by user corrections in order. "
            "Sensitive values are redacted.\n\n" + visible + "\n\n" + MARKER + encoded + "\n-->\n")
    if value["version"] == 2:
        body += EVIDENCE_MARKER + base64.b64encode(canonical(value)).decode("ascii") + "\n-->\n"
    if len(body) > MAX_BODY:
        raise ValueError("Task context exceeds the PR limit; do not truncate or summarize it.")
    return body


def parse(body: str) -> dict[str, Any]:
    if not isinstance(body, str) or len(body) > 65000 or body.count(MARKER) != 1:
        raise ValueError("PR must attach exactly one user task record.")
    encoded = body.split(MARKER, 1)[1].split("\n-->", 1)
    if len(encoded) != 2:
        raise ValueError("Incomplete task attachment.")
    try:
        original = validate(json.loads(base64.b64decode(encoded[0], validate=True)))
        if EVIDENCE_MARKER not in body:
            return original
        if body.count(EVIDENCE_MARKER) != 1:
            raise ValueError("Duplicate task evidence attachment.")
        supplement = body.split(EVIDENCE_MARKER, 1)[1].split("\n-->", 1)
        if len(supplement) != 2:
            raise ValueError("Incomplete task evidence attachment.")
        value = validate(json.loads(base64.b64decode(supplement[0], validate=True)))
        if value["version"] != 2 or legacy(value) != original:
            raise ValueError("Supplemental evidence changed the original task.")
        return value
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid task attachment.") from exc


def trailers(value: dict[str, Any], producer: str | None = None) -> str:
    actor = producer or value["producer_id"]
    if IDENTITY.fullmatch(actor) is None:
        raise ValueError("Invalid producer identity.")
    public = public_context(value)
    message = f"\n\n{TRAILER}: {digest(legacy(public))}\n{PRODUCER}: {actor}"
    if value["version"] == 2:
        message += f"\n{EVIDENCE_TRAILER}: {digest(public)}"
    return message


def verify_commit(value: dict[str, Any], message: str) -> str:
    hashes = re.findall(r"^" + TRAILER + r": ([0-9a-f]{64})$", message, re.M)
    actors = re.findall(r"^" + PRODUCER + r": ([A-Za-z0-9:-]{1,160})$", message, re.M)
    evidence_hashes = re.findall(r"^" + EVIDENCE_TRAILER + r": ([0-9a-f]{64})$", message, re.M)
    if (hashes != [digest(legacy(value))] or len(actors) != 1
            or evidence_hashes != ([digest(value)] if value["version"] == 2 else [])):
        raise ValueError("PR task context is not bound to its signed head commit.")
    return actors[0]


def repair_context(task_id: str, producer: str) -> dict[str, Any]:
    return {"version": 1, "task_id": task_id, "producer_id": producer,
            "source": "standing-integration-repair", "corrections": [],
            "starting_prompt": "User-authorized standing task: monitor advisory integration "
            "through completion and repair failures until green. Include fast non-integration "
            "regression coverage. Use GitHub for tests and captures; never run them on the Pi. "
            "Every repair requires a separate independent reviewer and fast CI. "
            "Integration success does not gate dev merges."}
