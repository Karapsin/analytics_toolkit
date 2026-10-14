"""Task-context wire format. Load only from the trusted controller checkout."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from typing import Any

MARKER = "<!-- agent-task-context-v1\n"
TRAILER = "Agent-Task-Context"
PRODUCER = "Agent-Producer"
MAX_BODY = 55000
FIELDS = {"version", "task_id", "producer_id", "source", "starting_prompt", "corrections"}
IDENTITY = re.compile(r"[A-Za-z0-9:-]{1,160}")


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != FIELDS or value["version"] != 1:
        raise ValueError("Missing or malformed user task context.")
    if any(not isinstance(value[k], str) or IDENTITY.fullmatch(value[k]) is None
           for k in ("task_id", "producer_id")):
        raise ValueError("Invalid task or producer identity.")
    if value["source"] not in {"user", "standing-integration-repair", "standing-python-support"}:
        raise ValueError("Unsupported task authority.")
    if not isinstance(value["starting_prompt"], str) or not value["starting_prompt"].strip():
        raise ValueError("The original user prompt is required; summaries are not substitutes.")
    if not isinstance(value["corrections"], list) or any(
        not isinstance(item, str) or not item.strip() for item in value["corrections"]
    ):
        raise ValueError("Task corrections must be nonempty user messages.")
    return value


def canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(validate(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


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
    return result


def render(value: dict[str, Any]) -> str:
    value = public_context(value)
    prompts = [value["starting_prompt"], *value["corrections"]]
    visible = "\n\n".join("<pre>" + html.escape(item) + "</pre>" for item in prompts)
    encoded = base64.b64encode(canonical(value)).decode("ascii")
    body = ("## User task\n\nStarting prompt, followed by user corrections in order. "
            "Sensitive values are redacted.\n\n" + visible + "\n\n" + MARKER + encoded + "\n-->\n")
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
        return validate(json.loads(base64.b64decode(encoded[0], validate=True)))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid task attachment.") from exc


def trailers(value: dict[str, Any], producer: str | None = None) -> str:
    actor = producer or value["producer_id"]
    if IDENTITY.fullmatch(actor) is None:
        raise ValueError("Invalid producer identity.")
    return f"\n\n{TRAILER}: {digest(public_context(value))}\n{PRODUCER}: {actor}"


def verify_commit(value: dict[str, Any], message: str) -> str:
    hashes = re.findall(r"^" + TRAILER + r": ([0-9a-f]{64})$", message, re.M)
    actors = re.findall(r"^" + PRODUCER + r": ([A-Za-z0-9:-]{1,160})$", message, re.M)
    if hashes != [digest(value)] or len(actors) != 1:
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
