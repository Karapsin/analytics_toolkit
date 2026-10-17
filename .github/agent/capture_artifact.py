"""Trusted GitHub artifact transport; failed diagnostics grant no visual receipt."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_FILES = 256


def _descend(descriptor: int, parts: tuple[str, ...]) -> int:
    current = os.dup(descriptor)
    try:
        for part in parts:
            if part in {"", ".", ".."}:
                raise ValueError("Unsafe diagnostic directory")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _directory(path: Path) -> int:
    if os.name != "posix":
        raise ValueError("Trusted GitHub artifact export requires POSIX no-follow descriptors")
    root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return _descend(root, path.absolute().parts[1:])
    finally:
        os.close(root)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _node(descriptor: int) -> tuple[int, int]:
    info = os.fstat(descriptor)
    return info.st_dev, info.st_ino


def _inventory(
    boundary: Path, *, max_files: int = MAX_FILES
) -> dict[str, tuple[str, tuple[int, ...]]]:
    root = _directory(boundary)
    pending: list[tuple[tuple[str, ...], int]] = [((), root)]
    result = {"": ("directory", _identity(os.fstat(root)))}
    files = directories = 0
    try:
        while pending:
            parts, descriptor = pending.pop()
            try:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        name_parts = (*parts, entry.name)
                        name = "/".join(name_parts)
                        if (
                            len(name_parts) > 8
                            or len(name) > 512
                            or "\\" in name
                            or entry.name in {"", ".", ".."}
                        ):
                            raise ValueError("Unsafe diagnostic inventory member")
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            directories += 1
                            if directories > 128:
                                raise ValueError("Diagnostic directory capacity exceeded")
                            child = os.open(
                                entry.name,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor,
                            )
                            pending.append((name_parts, child))
                            result[name] = ("directory", _identity(os.fstat(child)))
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            files += 1
                            if files > max_files or info.st_size > MAX_FILE:
                                raise ValueError("Diagnostic file capacity exceeded")
                            result[name] = ("file", _identity(info))
                        else:
                            raise ValueError("Unsafe diagnostic file type or hardlink")
            finally:
                os.close(descriptor)
        return result
    finally:
        for _, descriptor in pending:
            os.close(descriptor)


def _publish(destination: Path, records: list[tuple[str, bytes]]) -> None:
    """Create only new files through owned directory descriptors; never follow paths."""
    if len(records) > 258 or sum(len(data) for _, data in records) > MAX_TOTAL:
        raise ValueError("Artifact publication exceeds capacity")
    parent = _directory(destination.parent)
    root = None
    directories: dict[tuple[str, ...], tuple[int, int]] = {}
    names: set[str] = set()
    written: dict[str, tuple[int, ...]] = {}
    try:
        os.mkdir(destination.name, 0o700, dir_fd=parent)
        root = os.open(
            destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        directories[()] = _node(root)
        for name, data in records:
            parts = tuple(name.split("/"))
            if (
                name in names
                or len(parts) > 8
                or len(name) > 512
                or "\\" in name
                or any(part in {"", ".", ".."} for part in parts)
                or len(data) > MAX_FILE
            ):
                raise ValueError("Unsafe artifact member")
            names.add(name)
            directory = os.dup(root)
            try:
                for index, part in enumerate(parts[:-1]):
                    key = parts[: index + 1]
                    if key not in directories:
                        os.mkdir(part, 0o700, dir_fd=directory)
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                    os.close(directory)
                    directory = child
                    if key in directories and directories[key] != _node(child):
                        raise ValueError("Artifact destination directory changed")
                    directories[key] = _node(child)
                descriptor = os.open(
                    parts[-1],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=directory,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                    info = os.fstat(stream.fileno())
                    observed = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or _identity(info) != _identity(observed)
                    ):
                        raise ValueError("Artifact destination file changed")
                    written[name] = _identity(info)
            finally:
                os.close(directory)
        for parts, node in directories.items():
            descriptor = _descend(root, parts)
            try:
                if _node(descriptor) != node:
                    raise ValueError("Artifact destination tree changed")
            finally:
                os.close(descriptor)
        observed_root = os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
        repeated_parent = _directory(destination.parent)
        try:
            if _node(parent) != _node(repeated_parent) or (
                observed_root.st_dev,
                observed_root.st_ino,
            ) != _node(root):
                raise ValueError("Artifact destination root changed")
        finally:
            os.close(repeated_parent)
        observed_files = {
            name: identity
            for name, (kind, identity) in _inventory(destination, max_files=258).items()
            if kind == "file"
        }
        if observed_files != written:
            raise ValueError("Artifact destination membership or bytes changed")
    finally:
        if root is not None:
            os.close(root)
        os.close(parent)


def _regular(path: Path, boundary: Path) -> bytes:
    relative = path.relative_to(boundary)
    root = _directory(boundary)
    parent = descriptor = repeated = repeated_root = None
    try:
        parent = _descend(root, relative.parent.parts)
        descriptor = os.open(
            relative.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
            raise ValueError("Diagnostic file exceeds regular-file bounds")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            value = stream.read(MAX_FILE + 1)
        repeated = _descend(root, relative.parent.parts)
        repeated_root = _directory(boundary)
        if (
            len(value) != info.st_size
            or _identity(os.fstat(descriptor)) != _identity(info)
            or _identity(os.stat(relative.name, dir_fd=repeated, follow_symlinks=False))
            != _identity(info)
            or (os.fstat(parent).st_dev, os.fstat(parent).st_ino)
            != (os.fstat(repeated).st_dev, os.fstat(repeated).st_ino)
            or (os.fstat(root).st_dev, os.fstat(root).st_ino)
            != (os.fstat(repeated_root).st_dev, os.fstat(repeated_root).st_ino)
        ):
            raise ValueError("Diagnostic source changed during export")
        return value
    finally:
        for item in (descriptor, parent, repeated, repeated_root, root):
            if item is not None:
                os.close(item)


def export_failure(
    destination: Path,
    evidence: Path | None,
    *,
    identity: dict[str, Any],
    result: dict[str, Any],
    session: Path | None = None,
) -> dict[str, Any]:
    """Retain every run-owned byte, including a failed scene's pending entry."""
    if destination.exists() or destination.is_symlink():
        raise ValueError("Diagnostic destination already exists")
    if set(identity) != {"candidate", "repository", "run_id", "run_attempt"}:
        raise ValueError("Unexpected diagnostic identity fields")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", identity.get("repository", "")):
        raise ValueError("Invalid diagnostic repository")
    if result.get("ok") is not False:
        raise ValueError("Failure diagnostics require an explicit failure")
    if not re.fullmatch(r"[0-9a-f]{40}", identity.get("candidate", "")):
        raise ValueError("Invalid diagnostic candidate")
    for key in ("run_id", "run_attempt"):
        if type(identity.get(key)) is not int or identity[key] < 1:
            raise ValueError("Invalid GitHub run identity")
    records: list[tuple[str, bytes]] = []
    total = 0
    if session is not None:
        data = _regular(session, session.parent)
        records.append(("raw/session.json", data))
        total += len(data)
    if evidence is not None:
        if not evidence.is_dir() or evidence.is_symlink():
            raise ValueError("Missing run-owned diagnostic root")
        inventory = _inventory(evidence, max_files=MAX_FILES)
        for name in sorted(inventory):
            if inventory[name][0] != "file":
                continue
            source = evidence / name
            if len(records) >= MAX_FILES or total + inventory[name][1][2] > MAX_TOTAL:
                raise ValueError("Complete diagnostics exceed capacity; no truncation permitted")
            data = _regular(source, evidence)
            if "\\" in name or any(part in {"", ".", ".."} for part in name.split("/")):
                raise ValueError("Unsafe diagnostic member")
            records.append(("raw/evidence/" + name, data))
            total += len(data)
            if len(records) > MAX_FILES or total > MAX_TOTAL:
                raise ValueError("Complete diagnostics exceed capacity; no truncation permitted")
        if _inventory(evidence, max_files=MAX_FILES) != inventory:
            raise ValueError("Diagnostic source inventory changed during export")
    if session is not None and _regular(session, session.parent) != records[0][1]:
        raise ValueError("Diagnostic session changed during export")
    manifest = {
        "schema_version": 1,
        "kind": "failed-visual-capture-diagnostics",
        **identity,
        "capture_result": result,
        "collection_complete": evidence is not None and session is not None and bool(records),
        "session_file": "raw/session.json" if session is not None else None,
        "raw_evidence_prefix": "raw/evidence/",
        "visual_approval": False,
        "files": {
            name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in records
        },
    }
    outcomes = [
        json.loads(data).get("outputs_complete")
        for name, data in records
        if name.endswith("/execution.json")
    ]
    manifest["process_output_complete"] = (
        all(value is True for value in outcomes) if outcomes else None
    )
    encoded = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()
    if len(encoded) > 512 * 1024 or total + len(encoded) > MAX_TOTAL:
        raise ValueError("Diagnostic manifest exceeds capacity")
    _publish(destination, [*records, ("diagnostics.json", encoded)])
    return manifest


def capture_artifact(root: Path, identity: dict[str, Any]) -> bool:
    """Keep the old passing artifact and separately export honest failures."""
    evidence: Path | None = None
    session_file: Path | None = None
    result: dict[str, Any] = {"ok": False, "stage": "import"}
    try:
        from agent_tools.sql_explorer_visual import visual_workflow

        result = visual_workflow("start", root=str(root))
        if not result["ok"]:
            return False
        review = result["review_id"]
        if not re.fullmatch(r"review-[A-Za-z0-9_-]+", review):
            raise ValueError("Invalid run-owned review identity")
        # These roots belong to the trusted transport, not candidate-supplied paths.
        state_root = root / ".rag_index/sql-explorer-visual"
        session_file = state_root / "sessions" / (review + ".json")
        evidence = state_root / review
        result = visual_workflow("capture", review_id=review, root=str(root))
        if not result["ok"]:
            return False
        session = json.loads(_regular(session_file, session_file.parent))
        if session["capture"]["status"] != "pass":
            raise ValueError("Capture did not pass")
        records: list[tuple[str, bytes]] = []
        for name, scene in session["scenes"].items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("Unsafe scene name")
            for key, folder, suffix in (
                ("screenshot", "screenshots", ".png"),
                ("geometry", "geometry", ".json"),
            ):
                source = evidence / folder / (name + suffix)
                if Path(scene[key]) != source:
                    raise ValueError("Scene path escaped assigned evidence")
                records.append((name + suffix, _regular(source, evidence)))
        session["candidate"] = identity["candidate"]
        records.append(("capture.json", json.dumps(session).encode()))
        _publish(root / "visual-artifact", records)
        return True
    except Exception as exc:
        result = {"ok": False, "stage": "capture-or-export", "error": str(exc)}
        return False
    finally:
        if result.get("ok") is not True:
            destination = root / "visual-diagnostics"
            try:
                export_failure(
                    destination, evidence, session=session_file, identity=identity, result=result
                )
            except (ValueError, OSError) as exc:
                # Preserve honest collection failure when a safe empty destination remains.
                if destination.exists() or destination.is_symlink():
                    raise
                export_failure(
                    destination,
                    None,
                    identity=identity,
                    result={
                        "ok": False,
                        "stage": "diagnostic-export",
                        "error": str(exc),
                        "original_capture_result": result,
                    },
                )


def main() -> None:
    identity = {
        "candidate": os.environ["CANDIDATE"],
        "repository": os.environ["GITHUB_REPOSITORY"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
    }
    if not capture_artifact(Path.cwd(), identity):
        raise SystemExit("Visual capture failed; diagnostic artifact is separate from approval")


if __name__ == "__main__":
    main()
