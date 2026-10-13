"""Private, per-task user messages and automatic PR attachments."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import struct
import subprocess
import uuid
import zlib
from pathlib import Path
from typing import Any, NoReturn

MAX_IMAGE_BYTES = 512 * 1024
MAX_IMAGE_DIMENSION = 4096
PNG_HEADER_SIZE = 13
PNG_FILTER_MAX = 4
PNG_PALETTE_TYPE = 3


def protocol() -> Any:
    path = Path(__file__).resolve().parents[1] / ".github/agent/task_context.py"
    spec = importlib.util.spec_from_file_location("local_task_protocol", path)
    if spec is None or spec.loader is None:
        message = "Task protocol is unavailable."
        raise RuntimeError(message)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def directory(root: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        message = "Private task records require a Git checkout."
        raise RuntimeError(message)
    path = Path(result.stdout.strip()) / "atk-tasks"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def read(root: Path) -> dict[str, Any]:
    path = directory(root) / "active.json"
    if not path.is_file():
        message = (
            "Record the original user prompt with prepare_start(user_prompt=...) "
            "before submitting a feature PR."
        )
        raise RuntimeError(message)
    value: dict[str, Any] = protocol().validate(json.loads(path.read_text()))
    return value


def record(root: Path, prompt: str, *, append: bool = False) -> dict[str, Any]:
    if not isinstance(prompt, str) or not prompt.strip():
        message = "Supply the exact nonempty user message."
        raise ValueError(message)
    path = directory(root) / "active.json"
    if path.exists():
        value = read(root)
        if append:
            if not value["corrections"] or value["corrections"][-1] != prompt:
                value["corrections"].append(prompt)
        elif value["starting_prompt"] != prompt:
            message = (
                "Starting prompt is immutable; use task_context(action='append') "
                "for later user corrections."
            )
            raise ValueError(message)
    elif append:
        message = "Record the starting user prompt before corrections."
        raise ValueError(message)
    else:
        identity = uuid.uuid4().hex
        value = {
            "version": 1,
            "task_id": identity,
            "producer_id": "local:" + identity,
            "source": "user",
            "starting_prompt": prompt,
            "corrections": [],
        }
    return save(root, value)


def save(root: Path, value: dict[str, Any]) -> dict[str, Any]:
    path = directory(root) / "active.json"
    proto = protocol()
    proto.render(value)  # Check safe publication before saving or committing.
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)
    return {
        "task_id": value["task_id"],
        "digest": proto.digest(proto.public_context(value)),
        "correction_count": len(value["corrections"]),
    }


def evidence(root: Path, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Record exact evidence; caller must supply actual user messages, never invented approval."""
    value = read(root)
    if value["version"] == 1:
        value.update(version=2, plans=[], images=[], replacements=[], decisions=[], questions=[])
    value.setdefault("questions", [])
    proto = protocol()
    extras: dict[str, str] = {}
    if action == "plan":
        extras["plan_id"] = record_plan(value, payload, proto)
    elif action == "question":
        extras["question_id"] = record_question(value, payload, proto)
    elif action in {"approve-plan", "answer-question", "image", "replace-missing"}:
        index = approval_index(value, payload, proto)
        if action in {"approve-plan", "answer-question"}:
            bind_answer(value, payload, index, action)
        elif action == "replace-missing":
            value["replacements"].append(
                {"reference": payload.get("reference"), "user_index": index}
            )
        else:
            extras["evidence_path"] = image_evidence(root, value, payload, index, proto)
    else:
        fail("Unknown task evidence action.")
    result = save(root, value)
    result.update(extras)
    return result


def fail(message: str) -> NoReturn:
    raise ValueError(message)


def record_plan(value: dict[str, Any], payload: dict[str, Any], proto: Any) -> str:
    if set(payload) != {"text"} or not isinstance(payload["text"], str):
        fail("Supply the exact plan text.")
    identity: str = proto.plan_id(payload["text"])
    if not any(plan["id"] == identity for plan in value["plans"]):
        value["plans"].append({"id": identity, "text": payload["text"], "approval_index": None})
    return identity


def record_question(value: dict[str, Any], payload: dict[str, Any], proto: Any) -> str:
    if set(payload) != {"text"} or not isinstance(payload["text"], str):
        fail("Supply the exact question text, including every offered option.")
    questions = value["questions"]
    if (
        questions
        and questions[-1]["text"] == payload["text"]
        and questions[-1]["answer_index"] is None
    ):
        return str(questions[-1]["id"])
    identity: str = proto.question_id(payload["text"], len(questions))
    questions.append({"id": identity, "text": payload["text"], "answer_index": None})
    return identity


def bind_answer(value: dict[str, Any], payload: dict[str, Any], index: int, action: str) -> None:
    bindings = {
        "approve-plan": ("plans", "plan_id", "approval_index"),
        "answer-question": ("questions", "question_id", "answer_index"),
    }
    field, key, target = bindings[action]
    records = [item for item in value[field] if item["id"] == payload.get(key)]
    if len(records) != 1:
        fail("Answer or approval requires an existing exact evidence ID.")
    if records[0][target] is not None and records[0][target] != index:
        if action == "approve-plan":
            indices = records[0].setdefault("approval_indices", [])
            if index not in indices:
                indices.append(index)
            return
        fail("Existing evidence answers are immutable; record a new question or plan.")
    records[0][target] = index


def approval_index(value: dict[str, Any], payload: dict[str, Any], proto: Any) -> int:
    message = payload.get("user_prompt")
    if not isinstance(message, str) or not message.strip():
        fail("Supply the exact user approval or replacement message.")
    index = payload.get("correction_index")
    if index is not None:
        proto.user_index(value, index)
        if value["corrections"][index] != message:
            fail("Evidence approval does not match the recorded user message.")
    else:
        if not value["corrections"] or value["corrections"][-1] != message:
            value["corrections"].append(message)
        index = len(value["corrections"]) - 1
    return index


def image_evidence(
    root: Path, value: dict[str, Any], payload: dict[str, Any], index: int, proto: Any
) -> str:
    path = Path(payload.get("path", ""))
    if not path.is_absolute():
        path = root / path
    if path.is_symlink() or path.stat().st_size > MAX_IMAGE_BYTES:
        fail("Image must be a regular PNG of at most 512 KiB.")
    data = path.read_bytes()
    check_png(data)
    digest = hashlib.sha256(data).hexdigest()
    relative = "agent_tools/task_evidence/" + value["task_id"] + "/" + digest + ".png"
    value["images"].append(
        {
            "reference": payload.get("reference"),
            "description": payload.get("description"),
            "path": relative,
            "sha256": digest,
            "approval_index": index,
        }
    )
    proto.render(value)  # Validate everything before adding a publishable file.
    destination = root / relative
    if any(parent.is_symlink() for parent in [destination, *destination.parents]):
        fail("Image destination cannot contain symlinks.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return relative


def check_png(data: bytes) -> None:
    """Inspect only: require an already sanitized image; never modify its pixels."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        fail("Evidence must be a sanitized PNG.")
    position = 8
    chunks = []
    payloads: dict[bytes, list[bytes]] = {}
    while position + 12 <= len(data):
        size = int.from_bytes(data[position : position + 4], "big")
        end = position + 12 + size
        kind = data[position + 4 : position + 8]
        if (
            end > len(data)
            or kind not in {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"sRGB", b"gAMA", b"cHRM"}
            or zlib.crc32(data[position + 4 : end - 4])
            != int.from_bytes(data[end - 4 : end], "big")
        ):
            fail("Invalid PNG or metadata present; provide an owner-reviewed sanitized PNG.")
        chunks.append(kind)
        payloads.setdefault(kind, []).append(data[position + 8 : end - 4])
        if kind == b"IHDR":
            if size != PNG_HEADER_SIZE:
                fail("Invalid PNG header.")
            width, height = struct.unpack(">II", data[position + 8 : position + 16])
            if not 0 < width <= MAX_IMAGE_DIMENSION or not 0 < height <= MAX_IMAGE_DIMENSION:
                fail("Image dimensions exceed the review limit.")
        position = end
    if (
        position != len(data)
        or not chunks
        or chunks[0] != b"IHDR"
        or chunks[-1] != b"IEND"
        or b"IDAT" not in chunks
    ):
        fail("Incomplete PNG evidence.")
    check_raster(payloads)


def check_raster(payloads: dict[bytes, list[bytes]]) -> None:
    """Validate bounded, non-interlaced PNG scanlines without a new dependency."""
    if len(payloads[b"IHDR"]) != 1 or payloads[b"IEND"] != [b""]:
        fail("Invalid PNG header or end marker.")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", payloads[b"IHDR"][0]
    )
    depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if depth not in depths.get(color, set()) or compression or filtering or interlace:
        fail("Provide a valid non-interlaced PNG.")
    if color == PNG_PALETTE_TYPE and b"PLTE" not in payloads:
        fail("Indexed PNG evidence requires a palette.")
    stride = 1 + (width * channels[color] * depth + 7) // 8
    expected = height * stride
    try:
        decoder = zlib.decompressobj()
        raster = decoder.decompress(b"".join(payloads[b"IDAT"]), expected + 1)
    except zlib.error:
        fail("Invalid PNG image stream.")
    if len(raster) != expected or not decoder.eof or decoder.unused_data:
        fail("Invalid or oversized PNG image stream.")
    if any(raster[offset] > PNG_FILTER_MAX for offset in range(0, expected, stride)):
        fail("Invalid PNG scanline filter.")


def preflight(root: Path) -> None:
    value = read(root)
    protocol().require_resolved(value)
    for image in value.get("images", []):
        path = root / image["path"]
        if any(parent.is_symlink() for parent in [path, *path.parents]):
            fail("Image evidence cannot contain symlinks.")
        data = path.read_bytes()
        check_png(data)
        if hashlib.sha256(data).hexdigest() != image["sha256"]:
            fail("Image evidence differs from the owner-reviewed record.")
        result = subprocess.run(
            ["git", "show", "HEAD:" + image["path"]], cwd=root, capture_output=True, check=False
        )
        if result.returncode or result.stdout != data:
            fail("Image evidence must be committed unchanged in the signed head.")


def attachment(root: Path) -> str:
    return protocol().render(read(root))


def review_record(root: Path) -> dict[str, Any]:
    """Publish redacted evidence in the signed source tree during reader-first rollout."""
    value = read(root)
    proto = protocol()
    proto.require_resolved(value)
    relative = "agent_tools/task_evidence/" + value["task_id"] + "/context.json"
    destination = root / relative
    if any(parent.is_symlink() for parent in [destination, *destination.parents]):
        fail("Task evidence destination cannot contain symlinks.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(proto.public_context(value), ensure_ascii=False, indent=2) + "\n"
    )
    return {"evidence_path": relative, "digest": proto.digest(proto.public_context(value))}


def commit_trailers(root: Path) -> str:
    return protocol().trailers(read(root))


def finish(root: Path) -> None:
    path = directory(root) / "active.json"
    if path.exists():
        value = read(root)
        path.replace(path.with_name(value["task_id"] + ".json"))


def receipt(root: Path, action: str, prompt: str | None) -> dict[str, Any]:
    if action == "append" and prompt is not None:
        return record(root, prompt, append=True)
    if action != "status":
        message = "Use status or append with the exact user_prompt."
        raise ValueError(message)
    value = read(root)
    proto = protocol()
    return {
        "task_id": value["task_id"],
        "digest": proto.digest(proto.public_context(value)),
        "correction_count": len(value["corrections"]),
    }
