"""Private, per-task user messages and automatic PR attachments."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any


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


def attachment(root: Path) -> str:
    return protocol().render(read(root))


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
