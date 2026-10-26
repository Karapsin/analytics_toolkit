"""Trusted integration diagnostics and durable no-patch repair outcomes."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_ARCHIVE = 4 * 1024 * 1024
MAX_FILE = 512 * 1024
DIAGNOSTICS = {"startup.log", "build.log", "compose.log", "service-health.json"}
TRUNCATED = b"[truncated: earlier diagnostic output omitted]\n"


def inventory(gh: Any, runs: list[int]) -> list[dict[str, Any]]:
    result = []
    for run in sorted(runs):
        metadata = gh.api(f"actions/runs/{run}")
        artifacts = gh.pages(f"actions/runs/{run}/artifacts?per_page=100", "artifacts")
        result.append({"run": run, "attempt": metadata["run_attempt"],
                       "head": metadata["head_sha"], "artifacts": sorted([
                           {key: item[key] for key in
                            ("id", "name", "size_in_bytes", "expired", "created_at")}
                           for item in artifacts if item["name"].startswith("sql-integration-")
                           and item["created_at"] >= metadata["run_started_at"]
                       ], key=lambda item: item["id"])})
    return result


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def download(repository: str, identity: int) -> bytes:
    """Stream at most four MiB; credentials remain inside gh's environment."""
    args = ["gh", "api", f"repos/{repository}/actions/artifacts/{identity}/zip"]
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        timer = threading.Timer(60, process.kill)
        timer.start()
        try:
            if process.stdout is None:
                raise RuntimeError("Artifact download unavailable")
            content = process.stdout.read(MAX_ARCHIVE + 1)
            if len(content) > MAX_ARCHIVE:
                process.kill()
                raise ValueError("Integration artifact exceeds download limit")
            if process.wait() != 0:
                raise RuntimeError("Integration artifact download deferred")
            return content
        finally:
            timer.cancel()


def extract(content: bytes, destination: Path, redact: Any) -> dict[str, str]:
    files = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 100:
            raise ValueError("Integration artifact file limit exceeded")
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                    or ":" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("Unsafe integration artifact path")
            total += entry.file_size
            if total > MAX_ARCHIVE:
                raise ValueError("Integration artifact expansion limit exceeded")
            if entry.is_dir() or path.name not in DIAGNOSTICS:
                continue
            # The expansion bound above caps this read; redact the whole file
            # before keeping only its tail so no secret is split by the cut.
            text = archive.read(entry).decode("utf-8", errors="replace")
            text = re.sub(r"(?i)(Authorization\s*:\s*)([^\r\n]+)", r"\1[REDACTED]", text)
            text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
            for name in ("GH_TOKEN", "GITHUB_TOKEN"):
                if os.environ.get(name):
                    text = text.replace(os.environ[name], "[REDACTED]")
            data = redact(text).encode()
            if len(data) > MAX_FILE:
                # Failures are reported at the end of service logs; keep the tail.
                tail = data[-(MAX_FILE - len(TRUNCATED)):].decode("utf-8", errors="ignore")
                data = TRUNCATED + tail.encode()
            relative = path.as_posix()
            if relative in files:
                raise ValueError("Duplicate integration diagnostic path")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files[relative] = hashlib.sha256(data).hexdigest()
    return files


def prepare(gh: Any, task: dict[str, Any], destination: Path, redact: Any) -> None:
    snapshot = inventory(gh, task["runs"])
    if fingerprint(snapshot) != task["evidence_key"]:
        raise RuntimeError("Integration evidence changed; reconcile again")
    destination.mkdir(parents=True)
    files, missing = {}, []
    for run in snapshot:
        available = [item for item in run["artifacts"] if not item["expired"]]
        if not available:
            missing.append(f"Run {run['run']} attempt {run['attempt']}: artifacts missing or expired")
        for item in available:
            if item["size_in_bytes"] > MAX_ARCHIVE:
                missing.append(f"Artifact {item['id']}: exceeds download limit")
                continue
            prefix = f"run-{run['run']}/artifact-{item['id']}"
            try:
                content = download(gh.repository, item["id"])
                extracted = extract(content, destination / prefix, redact)
            except (ValueError, zipfile.BadZipFile) as error:
                # A limit or unsafe archive withholds only this artifact's files;
                # the repair still starts from the remaining evidence and logs.
                shutil.rmtree(destination / prefix, ignore_errors=True)
                missing.append(f"Artifact {item['id']}: evidence unavailable: {error}")
                continue
            files.update({prefix + "/" + name: digest for name, digest in extracted.items()})
            if not extracted:
                missing.append(f"Artifact {item['id']}: startup/build diagnostics unavailable")
    manifest = {"runs": snapshot, "files": files, "missing": missing,
                "evidence_key": task["evidence_key"]}
    (destination / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))


def connection() -> sqlite3.Connection:
    directory = Path(os.environ.get("AGENT_STATE_DIR", ".git/agent-state"))
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / "repair-outcomes.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS outcomes (identity TEXT PRIMARY KEY, value TEXT)")
    return db


def identity(task: dict[str, Any]) -> str:
    return fingerprint({key: task.get(key) for key in ("base", "runs", "evidence_key", "failure_key")})


def blocked(task: dict[str, Any]) -> bool:
    db = connection()
    try:
        return db.execute("SELECT 1 FROM outcomes WHERE identity=?", (identity(task),)).fetchone() is not None
    finally:
        db.close()


def record(task: dict[str, Any], outcome: dict[str, Any]) -> bool:
    db = connection()
    try:
        with db:
            cursor = db.execute("INSERT OR IGNORE INTO outcomes VALUES (?,?)",
                                (identity(task), json.dumps(outcome)))
        return cursor.rowcount == 1
    finally:
        db.close()
