#!/usr/bin/env python3
"""Headless current-host capture and agent review gate for SQL Explorer UI changes."""

# ruff: noqa: EM101, EM102, PLR0911, TRY003, TRY301

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

MANIFEST = Path("visual-tests/sql_explorer/scenes.json")
STATE_ROOT = Path(".rag_index/sql-explorer-visual")
RECEIPT = STATE_ROOT / "receipt.json"
SESSIONS = STATE_ROOT / "sessions"
CAPTURE_MODE = "textual-headless"
VIEWPORT = (1280, 800)
TERMINAL_COLUMNS = 208
TERMINAL_ROWS = 47
VERDICTS = {"pass", "product_defect", "infrastructure_failure"}
VISUAL_PATH_PREFIXES = (
    "analytics_toolkit/sql_explorer/",
    "agent_tools/sql_explorer_visual",
    "visual-tests/sql_explorer/",
)
SENSITIVE_NAMES = {".connections", ".env"}
SENSITIVE_PARTS = {".certs", ".rag_index", ".venv", "__pycache__"}


class VisualReviewError(RuntimeError):
    """Raised when deterministic visual evidence cannot be produced or trusted."""


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise VisualReviewError(f"expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    path.chmod(0o600)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VisualReviewError(f"{Path(command[0]).name} timed out after {timeout}s") from exc
    if check and completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise VisualReviewError(message)
    return completed


def _git(root: Path, *arguments: str) -> str:
    return _run(["git", *arguments], cwd=root).stdout


def _manifest(root: Path) -> dict[str, Any]:
    manifest = _read_json(root / MANIFEST)
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("scenes"), list):
        raise VisualReviewError("SQL Explorer visual manifest must use schema_version 1")
    scene_ids = [scene.get("id") for scene in manifest["scenes"]]
    if any(not isinstance(scene_id, str) or not scene_id for scene_id in scene_ids):
        raise VisualReviewError("every SQL Explorer visual scene needs a non-empty id")
    if len(scene_ids) != len(set(scene_ids)):
        raise VisualReviewError("SQL Explorer visual scene ids must be unique")
    return manifest


def tracked_content_paths(root: Path) -> list[str]:
    output = _run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
    ).stdout
    return sorted(path for path in output.split("\0") if path and not _sensitive(path))


def _sensitive(relative_path: str) -> bool:
    path = Path(relative_path)
    return path.name in SENSITIVE_NAMES or any(part in SENSITIVE_PARTS for part in path.parts)


def content_fingerprint(root: Path) -> str:
    """Hash the reviewable tree without binding the receipt to a local commit SHA."""
    digest = hashlib.sha256()
    for relative_path in tracked_content_paths(root):
        path = root / relative_path
        if path.is_symlink():
            content = os.readlink(path).encode("utf-8")
            mode = "symlink"
        elif path.is_file():
            content = path.read_bytes()
            mode = "executable" if os.access(path, os.X_OK) else "file"
        else:
            continue
        digest.update(relative_path.encode("utf-8") + b"\0")
        digest.update(mode.encode("ascii") + b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def changed_paths(root: Path, *, for_push: bool = False) -> list[str]:
    if for_push:
        comparison = _run(
            ["git", "diff", "--name-only", "origin/dev...HEAD"],
            cwd=root,
            check=False,
        )
        if comparison.returncode == 0:
            return sorted(path for path in comparison.stdout.splitlines() if path)
    try:
        status = _git(root, "status", "--porcelain=v1", "-z")
    except VisualReviewError:
        return []
    paths: list[str] = []
    entries = status.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        path = entry[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        paths.append(path)
        if entry[:1] in {"R", "C"} and index < len(entries):
            index += 1
    return sorted(set(paths))


def visual_review_required(paths: list[str]) -> bool:
    return any(any(path.startswith(prefix) for prefix in VISUAL_PATH_PREFIXES) for path in paths)


def verify_visual_receipt(
    root: Path,
    *,
    paths: list[str] | None = None,
    for_push: bool = False,
) -> dict[str, Any]:
    selected_paths = changed_paths(root, for_push=for_push) if paths is None else paths
    if not visual_review_required(selected_paths):
        return {"ok": True, "required": False, "message": "Visual review is not required."}
    receipt_path = root / RECEIPT
    if not receipt_path.is_file():
        return {
            "ok": False,
            "required": True,
            "message": "No complete SQL Explorer headless visual-review receipt exists.",
        }
    try:
        receipt = _read_json(receipt_path)
        fingerprint = content_fingerprint(root)
        manifest_hash = _sha256(root / MANIFEST)
    except (OSError, json.JSONDecodeError, VisualReviewError) as exc:
        return {"ok": False, "required": True, "message": f"Visual receipt is unreadable: {exc}"}
    if receipt.get("capture_mode") != CAPTURE_MODE:
        return {
            "ok": False,
            "required": True,
            "message": "Visual receipt uses an obsolete capture method.",
        }
    if receipt.get("content_fingerprint") != fingerprint:
        return {
            "ok": False,
            "required": True,
            "message": "SQL Explorer visual-review receipt is stale for the current content.",
        }
    if receipt.get("manifest_sha256") != manifest_hash:
        return {
            "ok": False,
            "required": True,
            "message": "SQL Explorer visual-review receipt uses a different scene manifest.",
        }
    scenes = receipt.get("scenes", {})
    required_ids = {scene["id"] for scene in _manifest(root)["scenes"]}
    if set(scenes) != required_ids or any(
        scene.get("verdict") != "pass" for scene in scenes.values()
    ):
        return {
            "ok": False,
            "required": True,
            "message": (
                "SQL Explorer visual-review receipt is incomplete or contains a non-pass scene."
            ),
        }
    return {
        "ok": True,
        "required": True,
        "message": "SQL Explorer visual-review receipt matches the current content.",
        "review_id": receipt.get("review_id"),
        "receipt_sha256": _sha256(receipt_path),
    }


def session_path(root: Path, review_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{8,64}", review_id):
        raise VisualReviewError("review_id must contain 8-64 lowercase letters, digits, or hyphens")
    return root / SESSIONS / f"{review_id}.json"


def _load_session(root: Path, review_id: str) -> dict[str, Any]:
    path = session_path(root, review_id)
    if not path.is_file():
        raise VisualReviewError(f"SQL Explorer visual review does not exist: {review_id}")
    session = _read_json(path)
    if session.get("review_id") != review_id:
        raise VisualReviewError("SQL Explorer visual-review session id mismatch")
    return session


def start_review(root: Path, review_id: str | None = None) -> dict[str, Any]:
    manifest = _manifest(root)
    selected_id = review_id or f"review-{uuid4().hex[:12]}"
    path = session_path(root, selected_id)
    if path.exists():
        raise VisualReviewError(f"SQL Explorer visual review already exists: {selected_id}")
    session = {
        "schema_version": 1,
        "review_id": selected_id,
        "content_fingerprint": content_fingerprint(root),
        "manifest_sha256": _sha256(root / MANIFEST),
        "capture_mode": CAPTURE_MODE,
        "host": {"system": platform.system(), "python": platform.python_version()},
        "viewport": list(VIEWPORT),
        "created_at_epoch": int(time.time()),
        "capture": {"status": "pending", "workspace_removed": False},
        "scenes": {
            scene["id"]: {
                "capture": "pending",
                "screenshot": "",
                "screenshot_sha256": "",
                "geometry": "",
                "geometry_sha256": "",
                "verdict": "pending",
                "notes": "",
            }
            for scene in manifest["scenes"]
        },
    }
    _write_json(path, session)
    return session


def review_status(root: Path, review_id: str) -> dict[str, Any]:
    session = _load_session(root, review_id)
    pending = [
        scene_id
        for scene_id, scene in session["scenes"].items()
        if scene["capture"] == "pass" and scene["verdict"] == "pending"
    ]
    batch_size = int(_manifest(root).get("review_batch_size", 5))
    return {
        "review_id": review_id,
        "content_current": session["content_fingerprint"] == content_fingerprint(root),
        "capture": session["capture"],
        "scene_count": len(session["scenes"]),
        "pending_review_count": len(pending),
        "next_batch": [
            {
                "scene_id": scene_id,
                "screenshot": session["scenes"][scene_id]["screenshot"],
                "geometry": session["scenes"][scene_id]["geometry"],
            }
            for scene_id in pending[:batch_size]
        ],
    }


def record_review(
    root: Path,
    review_id: str,
    scene_id: str,
    verdict: str,
    notes: str | None = None,
) -> dict[str, Any]:
    if verdict not in VERDICTS:
        raise VisualReviewError("verdict must be pass, product_defect, or infrastructure_failure")
    if verdict != "pass" and not (notes or "").strip():
        raise VisualReviewError("a non-pass visual verdict requires notes")
    session = _load_session(root, review_id)
    if scene_id not in session["scenes"]:
        raise VisualReviewError(f"unknown SQL Explorer visual scene: {scene_id}")
    if session["scenes"][scene_id]["capture"] != "pass":
        raise VisualReviewError(f"scene capture is not valid: {scene_id}")
    session["scenes"][scene_id]["verdict"] = verdict
    session["scenes"][scene_id]["notes"] = (notes or "").strip()
    session["updated_at_epoch"] = int(time.time())
    _write_json(session_path(root, review_id), session)
    return review_status(root, review_id)


def complete_review(root: Path, review_id: str) -> dict[str, Any]:
    session = _load_session(root, review_id)
    if session["content_fingerprint"] != content_fingerprint(root):
        raise VisualReviewError("SQL Explorer visual review is stale for the current content")
    incomplete = [
        scene_id
        for scene_id, scene in session["scenes"].items()
        if scene["capture"] != "pass" or scene["verdict"] != "pass"
    ]
    if incomplete:
        raise VisualReviewError(
            "visual review has incomplete/non-pass scenes: " + ", ".join(incomplete)
        )
    if session["capture"].get("status") != "pass":
        raise VisualReviewError("headless visual capture did not complete")
    if session["capture"].get("workspace_removed") is not True:
        raise VisualReviewError("temporary visual-review workspace was not removed")
    receipt = {
        "schema_version": 1,
        "review_id": review_id,
        "content_fingerprint": session["content_fingerprint"],
        "manifest_sha256": session["manifest_sha256"],
        "capture_mode": session["capture_mode"],
        "host": session["host"],
        "viewport": session["viewport"],
        "completed_at_epoch": int(time.time()),
        "scenes": {
            scene_id: {
                "screenshot_sha256": scene["screenshot_sha256"],
                "geometry_sha256": scene["geometry_sha256"],
                "verdict": scene["verdict"],
                "notes": scene["notes"],
            }
            for scene_id, scene in session["scenes"].items()
        },
    }
    _write_json(root / RECEIPT, receipt)
    return verify_visual_receipt(root, paths=["analytics_toolkit/sql_explorer/app.py"])


def _copy_review_tree(root: Path, destination: Path) -> None:
    for relative_path in tracked_content_paths(root):
        source = root / relative_path
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            shutil.copy2(source, target)


def _wait_geometry(path: Path, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        if path.is_file():
            latest = _read_json(path)
            if latest.get("ok") is True:
                screen = latest.get("screen", {})
                if (
                    screen.get("height", 0) > TERMINAL_ROWS
                    or screen.get("width", 0) > TERMINAL_COLUMNS
                ):
                    raise VisualReviewError("Terminal grid extends beyond the capture viewport")
                return latest
        time.sleep(0.25)
    if latest is not None:
        return latest
    raise VisualReviewError(f"visual scene did not publish geometry: {path.name}")


def _validate_png(path: Path) -> None:
    from PIL import Image, ImageStat  # noqa: PLC0415 - optional agent capture dependency.

    with Image.open(path) as image:
        image.load()
        if image.size != VIEWPORT:
            raise VisualReviewError(f"Headless screenshot must be 1280x800, got {image.size}")
        extrema = ImageStat.Stat(image.convert("RGB")).extrema
        if all(low == high for low, high in extrema):
            raise VisualReviewError("Headless screenshot is blank")


def _host_python(root: Path) -> str:
    """Prefer the checkout environment using each platform's venv layout."""
    folder, executable = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = root / ".venv" / folder / executable
    return str(candidate) if candidate.is_file() else sys.executable


def capture_review(root: Path, review_id: str) -> dict[str, Any]:
    session = _load_session(root, review_id)
    if session["content_fingerprint"] != content_fingerprint(root):
        raise VisualReviewError("visual-review content changed after the session started")
    if session.get("capture_mode") != CAPTURE_MODE:
        raise VisualReviewError("start a new headless visual-review session")
    if session["capture"]["status"] != "pending":
        raise VisualReviewError("capture already attempted; start a new review")
    evidence_root = root / STATE_ROOT / review_id
    screenshots = evidence_root / "screenshots"
    geometry_root = evidence_root / "geometry"
    screenshots.mkdir(parents=True, exist_ok=True)
    geometry_root.mkdir(parents=True, exist_ok=True)
    python = _host_python(root)
    session["capture"] = {"status": "running", "workspace_removed": False}
    _write_json(session_path(root, review_id), session)
    workspace: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix=f"sql-explorer-visual-{review_id}-") as folder:
            workspace = Path(folder)
            checkout = workspace / "checkout"
            checkout.mkdir()
            _copy_review_tree(root, checkout)
            for scene_id, scene in session["scenes"].items():
                screenshot = screenshots / f"{scene_id}.png"
                geometry = geometry_root / f"{scene_id}.json"
                _run(
                    [
                        python,
                        "-m",
                        "agent_tools.sql_explorer_visual_capture",
                        "--scene",
                        scene_id,
                        "--evidence",
                        str(geometry),
                        "--manifest",
                        str(checkout / MANIFEST),
                        "--screenshot",
                        str(screenshot),
                    ],
                    cwd=checkout,
                    timeout=60,
                )
                payload = _wait_geometry(geometry, timeout=1)
                if payload.get("scene_id") != scene_id or payload.get("ok") is not True:
                    raise VisualReviewError(f"scene {scene_id} failed geometry checks")
                _validate_png(screenshot)
                scene.update(
                    capture="pass",
                    screenshot=str(screenshot),
                    screenshot_sha256=_sha256(screenshot),
                    geometry=str(geometry),
                    geometry_sha256=_sha256(geometry),
                )
                session["updated_at_epoch"] = int(time.time())
                _write_json(session_path(root, review_id), session)
        session["capture"]["status"] = "pass"
    except Exception as exc:
        session["capture"]["status"] = "failed"
        session["capture"]["error"] = str(exc)
        raise
    finally:
        session["capture"]["workspace_removed"] = workspace is None or not workspace.exists()
        session["updated_at_epoch"] = int(time.time())
        _write_json(session_path(root, review_id), session)
    return review_status(root, review_id)


def visual_workflow(action: str, review_id: str | None = None, root: str = ".") -> dict[str, Any]:
    root_path = Path(root).resolve()
    try:
        if action == "start":
            result = start_review(root_path, review_id)
        elif action == "capture":
            if review_id is None:
                raise VisualReviewError("review_id is required for capture")
            result = capture_review(root_path, review_id)
        elif action == "status":
            if review_id is None:
                result = verify_visual_receipt(root_path)
            else:
                result = review_status(root_path, review_id)
        elif action == "complete":
            if review_id is None:
                raise VisualReviewError("review_id is required for completion")
            result = complete_review(root_path, review_id)
        else:
            raise VisualReviewError("action must be start, capture, status, or complete")
    except (OSError, json.JSONDecodeError, VisualReviewError) as exc:
        return {"ok": False, "error": str(exc), "action": action, "review_id": review_id}
    return {
        "ok": True,
        "action": action,
        "review_id": result.get("review_id", review_id),
        "result": result,
    }


def visual_review(
    review_id: str,
    scene_id: str,
    verdict: str,
    notes: str | None = None,
    root: str = ".",
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    try:
        result = record_review(root_path, review_id, scene_id, verdict, notes)
    except (OSError, json.JSONDecodeError, VisualReviewError) as exc:
        return {"ok": False, "error": str(exc), "review_id": review_id, "scene_id": scene_id}
    return {"ok": True, "review_id": review_id, "scene_id": scene_id, "result": result}


__all__ = [
    "CAPTURE_MODE",
    "MANIFEST",
    "RECEIPT",
    "VisualReviewError",
    "changed_paths",
    "complete_review",
    "content_fingerprint",
    "record_review",
    "start_review",
    "verify_visual_receipt",
    "visual_review",
    "visual_review_required",
    "visual_workflow",
]
