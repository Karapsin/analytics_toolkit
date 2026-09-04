#!/usr/bin/env python3
"""Fresh-macOS-VM capture and agent review gate for SQL Explorer UI changes."""

# ruff: noqa: EM101, EM102, PLR0911, PLR0913, S108, TRY003, TRY301

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

MANIFEST = Path("visual-tests/sql_explorer/scenes.json")
STATE_ROOT = Path(".rag_index/sql-explorer-visual")
RECEIPT = STATE_ROOT / "receipt.json"
SESSIONS = STATE_ROOT / "sessions"
MACOS_IMAGE = (
    "ghcr.io/cirruslabs/macos-sequoia-base@"
    "sha256:fdd8b72a6ee46fc8ad35dc1b9f3b1f162b6607b82a584947d20bb28d3dcb99ed"
)
VIEWPORT = (1280, 800)
OPAQUE_BLACK_NS_COLOR = (
    "YnBsaXN0MDDUAQIDBAUGBwpYJHZlcnNpb25ZJGFyY2hpdmVyVCR0b3BYJG9iamVjdHMSAAGGoF8Q"
    "D05TS2V5ZWRBcmNoaXZlctEICVRyb290gAGjCwwTVSRudWxs0w0ODxAREldOU1doaXRlXE5TQ29s"
    "b3JTcGFjZVYkY2xhc3NEMCAxABADgALSFBUWF1okY2xhc3NuYW1lWCRjbGFzc2VzV05TQ29sb3Ki"
    "FhhYTlNPYmplY3QIERokKTI3SUxRU1ddZGx5gIWHiY6ZoqqtAAAAAAAAAQEAAAAAAAAAGQAAAAAA"
    "AAAAAAAAAAAAALY="
)
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
            "message": "No complete SQL Explorer macOS visual-review receipt exists.",
        }
    try:
        receipt = _read_json(receipt_path)
        fingerprint = content_fingerprint(root)
        manifest_hash = _sha256(root / MANIFEST)
    except (OSError, json.JSONDecodeError, VisualReviewError) as exc:
        return {"ok": False, "required": True, "message": f"Visual receipt is unreadable: {exc}"}
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
        "image": MACOS_IMAGE,
        "viewport": list(VIEWPORT),
        "created_at_epoch": int(time.time()),
        "capture": {"status": "pending", "vm_name": "", "vm_deleted": False},
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
    if session["capture"].get("vm_deleted") is not True:
        raise VisualReviewError("fresh visual-review VM was not deleted after capture")
    receipt = {
        "schema_version": 1,
        "review_id": review_id,
        "content_fingerprint": session["content_fingerprint"],
        "manifest_sha256": session["manifest_sha256"],
        "image": session["image"],
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


def _wait_for_vnc_url(
    process: subprocess.Popen[str], timeout: float = 120.0
) -> tuple[str, int, str]:
    if process.stdout is None:
        raise VisualReviewError("Tart VNC process has no output stream")
    deadline = time.monotonic() + timeout
    transcript: list[str] = []
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise VisualReviewError(
                "Tart VM exited before VNC became ready: " + "".join(transcript)
            )
        readable, _, _ = select.select([process.stdout], [], [], 1.0)
        if not readable:
            continue
        line = process.stdout.readline()
        transcript.append(line)
        match = re.search(r"vnc://[^\s]+", line)
        if not match:
            continue
        parsed = urlsplit(match.group(0))
        if not parsed.hostname or not parsed.port:
            raise VisualReviewError("Tart returned an invalid VNC URL")
        return parsed.hostname, parsed.port, unquote(parsed.password or "")
    raise VisualReviewError("timed out waiting for Tart experimental VNC URL")


def _wait_guest(root: Path, vm_name: str, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    failures: list[str] = []
    while time.monotonic() < deadline:
        result = _run(["tart", "exec", vm_name, "/usr/bin/true"], cwd=root, check=False)
        if result.returncode == 0:
            return
        failures.append(result.stderr.strip() or result.stdout.strip())
        time.sleep(2)
    raise VisualReviewError(
        "fresh macOS VM guest agent did not become ready: " + "; ".join(failures[-3:])
    )


def _guest_shell(root: Path, vm_name: str, script: str, timeout: int = 900) -> None:
    _run(["tart", "exec", vm_name, "/bin/zsh", "-lc", script], cwd=root, timeout=timeout)


def _prepare_guest(root: Path, vm_name: str, guest_root: str, review_id: str) -> str:
    venv = f"/tmp/analytics-toolkit-visual-{review_id}"
    dependencies = " ".join(
        shlex.quote(value)
        for value in (
            "numpy>=1.24,<2",
            "pandas>=1.4.4,<3",
            "pyperclip>=1.11,<2",
            "sqlglot>=26.33,<31",
            "sqlparse>=0.4.3,<1",
            "textual>=0.73,<0.74; python_version < '3.13'",
            "textual[syntax]>=0.89.1,<0.90; python_version >= '3.13'",
            "tqdm>=4.65,<5",
            "tree-sitter>=0.20.1,<0.21.0; python_version < '3.13'",
            "tree-sitter-languages==1.10.2; python_version < '3.13'",
            "tree-sitter>=0.23,<0.24; python_version >= '3.13'",
            "tree-sitter-sql>=0.3,<0.3.8; python_version >= '3.13'",
            "typing-extensions>=4.8",
        )
    )
    script = (
        "set -e; "
        "python3 -c 'import sys; assert sys.version_info >= (3, 10), sys.version'; "
        f"python3 -m venv {shlex.quote(venv)}; "
        f"{shlex.quote(venv)}/bin/python -m pip install --quiet --disable-pip-version-check "
        f"{dependencies}; "
        f"test -f {shlex.quote(guest_root)}/agent_tools/sql_explorer_visual_scene.py"
    )
    _guest_shell(root, vm_name, script, timeout=20 * 60)
    return venv


def _configure_guest_ui(root: Path, vm_name: str) -> None:
    terminal_preferences = "/tmp/sql-explorer-visual-terminal.plist"
    _guest_shell(
        root,
        vm_name,
        f"defaults export com.apple.Terminal {terminal_preferences} >/dev/null; "
        f"plutil -replace 'Window Settings.Pro.BackgroundColor' -data "
        f"{OPAQUE_BLACK_NS_COLOR} {terminal_preferences}; "
        f"defaults import com.apple.Terminal {terminal_preferences} >/dev/null; "
        "defaults write com.apple.Terminal 'Default Window Settings' -string Pro; "
        "defaults write com.apple.Terminal 'Startup Window Settings' -string Pro; "
        "defaults write com.apple.dock autohide -bool true; "
        "killall Dock >/dev/null 2>&1 || true; "
        "killall Terminal >/dev/null 2>&1 || true",
        timeout=60,
    )


def _open_terminal_scene(
    root: Path,
    vm_name: str,
    *,
    scene_id: str,
    guest_root: str,
    guest_output: str,
    venv: str,
) -> None:
    command = (
        "printf '\\033[3;0;24t\\033[8;61;208t'; sleep 1; "
        f"cd {shlex.quote(guest_root)} && "
        "export TERM=xterm-256color TEXTUAL_COLOR_SYSTEM=256 "
        "SQL_EXPLORER_VISUAL_REQUIRE_COLOR_256=1; unset COLORTERM; "
        f"exec {shlex.quote(venv)}/bin/python -m agent_tools.sql_explorer_visual_scene "
        f"--scene {shlex.quote(scene_id)} "
        f"--evidence {shlex.quote(guest_output + '/' + scene_id + '.json')} "
        f"--manifest {shlex.quote(guest_root + '/' + MANIFEST.as_posix())}"
    )
    launcher = f"/tmp/sql-explorer-visual-{scene_id}.command"
    launcher_content = "#!/bin/zsh\n" + command
    script = (
        f"printf '%s\\n' {shlex.quote(launcher_content)} > {shlex.quote(launcher)}; "
        f"chmod 700 {shlex.quote(launcher)}; "
        f"open -na Terminal {shlex.quote(launcher)}"
    )
    _guest_shell(root, vm_name, script, timeout=60)


def _wait_geometry(path: Path, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        if path.is_file():
            latest = _read_json(path)
            if latest.get("ok") is True:
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
            raise VisualReviewError(f"VNC screenshot must be 1280x800, got {image.size}")
        extrema = ImageStat.Stat(image.convert("RGB")).extrema
        if all(low == high for low, high in extrema):
            raise VisualReviewError("VNC screenshot is blank")


def _capture_frame(root: Path, server: str, port: int, password: str, output: Path) -> None:
    client = root / ".venv/bin/vncdo"
    if not client.is_file():
        raise VisualReviewError(".venv/bin/vncdo is required; install agent requirements")
    _run(
        [
            str(client),
            "-s",
            f"{server}::{port}",
            "-p",
            password,
            "--nocursor",
            "capture",
            str(output),
        ],
        cwd=root,
        timeout=60,
    )
    _validate_png(output)


def _record_capture_failure(
    session: dict[str, Any], evidence_root: Path, guest_output_host: Path, exc: Exception
) -> None:
    session["capture"]["status"] = "failed"
    session["capture"]["error"] = str(exc)
    diagnostic_root = evidence_root / "diagnostics"
    if guest_output_host.is_dir():
        shutil.copytree(guest_output_host, diagnostic_root, dirs_exist_ok=True)


def capture_review(root: Path, review_id: str) -> dict[str, Any]:  # noqa: PLR0915
    session = _load_session(root, review_id)
    if session["content_fingerprint"] != content_fingerprint(root):
        raise VisualReviewError("visual-review content changed after the session started")
    if shutil.which("tart") is None:
        raise VisualReviewError("Tart is required for SQL Explorer macOS visual review")
    vm_name = f"analytics-toolkit-sql-explorer-visual-{review_id}"
    collision = _run(["tart", "get", vm_name], cwd=root, check=False)
    if collision.returncode == 0:
        raise VisualReviewError(f"refusing to use pre-existing Tart VM: {vm_name}")

    evidence_root = root / STATE_ROOT / review_id
    screenshots = evidence_root / "screenshots"
    geometry_root = evidence_root / "geometry"
    screenshots.mkdir(parents=True, exist_ok=True)
    geometry_root.mkdir(parents=True, exist_ok=True)
    share = Path(tempfile.mkdtemp(prefix=f"sql-explorer-visual-{review_id}-"))
    checkout = share / "checkout"
    guest_output_host = share / "output"
    checkout.mkdir()
    guest_output_host.mkdir()
    _copy_review_tree(root, checkout)
    guest_root = "/Volumes/My Shared Files/review/checkout"
    guest_output = "/Volumes/My Shared Files/review/output"
    process: subprocess.Popen[str] | None = None
    owned = True
    session["capture"] = {"status": "running", "vm_name": vm_name, "vm_deleted": False}
    _write_json(session_path(root, review_id), session)
    try:
        _run(["tart", "clone", MACOS_IMAGE, vm_name], cwd=root, timeout=30 * 60)
        _run(
            ["tart", "set", vm_name, "--display", "1280x800px", "--no-display-refit"],
            cwd=root,
        )
        process = subprocess.Popen(
            [
                "tart",
                "run",
                "--vnc-experimental",
                "--no-graphics",
                "--no-audio",
                "--no-clipboard",
                f"--dir=review:{share}",
                vm_name,
            ],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        server, port, password = _wait_for_vnc_url(process)
        _wait_guest(root, vm_name)
        _configure_guest_ui(root, vm_name)
        venv = _prepare_guest(root, vm_name, guest_root, review_id)

        for scene_id, scene in session["scenes"].items():
            _guest_shell(
                root,
                vm_name,
                "pkill -f '[a]gent_tools.sql_explorer_visual_scene' >/dev/null 2>&1 || true; "
                "killall Terminal >/dev/null 2>&1 || true",
                timeout=30,
            )
            geometry_shared = guest_output_host / f"{scene_id}.json"
            geometry_shared.unlink(missing_ok=True)
            _open_terminal_scene(
                root,
                vm_name,
                scene_id=scene_id,
                guest_root=guest_root,
                guest_output=guest_output,
                venv=venv,
            )
            geometry = _wait_geometry(geometry_shared, timeout=45)
            if geometry.get("scene_id") != scene_id or geometry.get("ok") is not True:
                failed = [
                    name for name, passed in geometry.get("assertions", {}).items() if not passed
                ]
                raise VisualReviewError(
                    f"scene {scene_id} failed geometry checks: {', '.join(failed)}"
                )
            screenshot_path = screenshots / f"{scene_id}.png"
            _capture_frame(root, server, port, password, screenshot_path)
            geometry_path = geometry_root / f"{scene_id}.json"
            shutil.copy2(geometry_shared, geometry_path)
            scene.update(
                {
                    "capture": "pass",
                    "screenshot": str(screenshot_path),
                    "screenshot_sha256": _sha256(screenshot_path),
                    "geometry": str(geometry_path),
                    "geometry_sha256": _sha256(geometry_path),
                }
            )
            session["updated_at_epoch"] = int(time.time())
            _write_json(session_path(root, review_id), session)
        session["capture"]["status"] = "pass"
    except Exception as exc:
        _record_capture_failure(session, evidence_root, guest_output_host, exc)
        raise
    finally:
        if process is not None and process.poll() is None:
            _run(["tart", "stop", vm_name], cwd=root, timeout=120, check=False)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.terminate()
        if owned:
            deleted = _run(["tart", "delete", vm_name], cwd=root, timeout=120, check=False)
            session["capture"]["vm_deleted"] = deleted.returncode == 0
        shutil.rmtree(share, ignore_errors=True)
        session["capture"]["vm_name"] = vm_name
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
    "MACOS_IMAGE",
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
