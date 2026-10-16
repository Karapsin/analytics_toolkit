"""Lossless diagnostic artifacts, separate from quality-debt policy."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import uuid
from importlib import metadata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def checkout_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def toolchain() -> dict[str, str | None]:
    versions: dict[str, str | None] = {"python": platform.python_version()}
    for name in ("ruff", "mypy", "textual", "rich"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def retain(
    command: Sequence[str],
    root: Path,
    returncode: int | None,
    stdout: str,
    stderr: str,
) -> Path:
    """Write complete streams before their consumer can reject or aggregate them."""
    directory = root / ".rag_index" / "quality-diagnostics" / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    outputs = {}
    for stream, text in (("stdout", stdout), ("stderr", stderr)):
        payload = text.encode("utf-8")
        name = stream + ".txt"
        (directory / name).write_bytes(payload)
        outputs[stream] = {
            "path": name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    index = directory / "index.json"
    index.write_text(
        json.dumps(
            {
                "version": 1,
                "command": list(command),
                "cwd": str(root.resolve()),
                "returncode": returncode,
                "checkout_revision": checkout_revision(root),
                "source_head": os.environ.get("ATK_SOURCE_HEAD"),
                "workflow_sha": os.environ.get("GITHUB_SHA"),
                "run_id": os.environ.get("GITHUB_RUN_ID"),
                "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "job": os.environ.get("GITHUB_JOB"),
                "toolchain": toolchain(),
                "outputs": outputs,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Complete quality diagnostics: {index.relative_to(root)}")
    return index
