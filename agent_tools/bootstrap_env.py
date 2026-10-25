"""Build a reusable dependency wheelhouse; every session retains its own venv."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import shutil
import sys
import sysconfig
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from release_routines.lib.project_metadata import load_project

process = importlib.import_module(
    f"{__package__}.bootstrap_process" if __package__ else "bootstrap_process"
)
CACHE_VERSION = 1
GENERATION_LENGTH = 32
PROJECT_TOOLS = ["pytest", "tox", "virtualenv>=21.1,<21.5"]


def cache_root() -> Path | None:
    value = os.environ.get("ATK_BOOTSTRAP_CACHE_DIR")
    return Path(value).expanduser().resolve() if value else None


def fingerprint(root: Path, *, project: bool) -> str:
    digest = hashlib.sha256()
    runtime = (
        CACHE_VERSION,
        sys.version,
        sys.implementation.cache_tag,
        sysconfig.get_platform(),
        sysconfig.get_config_var("SOABI"),
        platform.machine(),
        project,
        PROJECT_TOOLS,
    )
    digest.update(repr(runtime).encode())
    for name in ("agent_tools/requirements-mcp.txt", "pyproject.toml", "tox.ini"):
        path = root / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
    return digest.hexdigest()


def _build_requirements(root: Path) -> list[str]:
    try:
        parser = importlib.import_module("tomllib")
    except ModuleNotFoundError:  # Python 3.10 bootstrap always has pip.
        parser = importlib.import_module("pip._vendor.tomli")
    data = parser.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return [*data["build-system"]["requires"], "wheel"]


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ready(directory: Path, key: str) -> Path | None:
    try:
        manifest = json.loads((directory / "ready.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return None
        generation = manifest["generation"]
        if (
            not isinstance(generation, str)
            or manifest["key"] != key
            or len(generation) != GENERATION_LENGTH
            or any(char not in "0123456789abcdef" for char in generation)
        ):
            return None
        wheels = directory / generation
        files = manifest["files"]
        if (
            not isinstance(files, dict)
            or not files
            or set(files) != {path.name for path in wheels.iterdir()}
        ):
            return None
        for name, expected in files.items():
            if (
                Path(name).name != name
                or not name.endswith(".whl")
                or _sha(wheels / name) != expected
            ):
                return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return wheels


def _publish(directory: Path, wheels: Path, key: str) -> None:
    manifest = {
        "key": key,
        "generation": wheels.name,
        "files": {path.name: _sha(path) for path in wheels.iterdir()},
    }
    temporary = directory / f".{uuid.uuid4().hex}.json"
    temporary.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(directory / "ready.json")


def _offline(wheels: Path) -> dict[str, str]:
    # Also propagate the local-only policy into pip's isolated build subprocesses.
    return {"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": wheels.as_uri(), "PIP_EXTRA_INDEX_URL": ""}


def _health(root: Path, *, project: bool) -> list[dict[str, Any]]:
    python = str(root / ".venv/bin/python")
    imports = "import mcp"
    if project:
        imports += (
            "; import analytics_toolkit, pytest, tox; from pathlib import Path; "
            "Path(analytics_toolkit.__file__).resolve().relative_to(Path.cwd().resolve())"
        )
    return [
        {
            "display": "Verify installed dependency compatibility",
            "args": [python, "-m", "pip", "check"],
        },
        {"display": "Verify session environment imports", "args": [python, "-c", imports]},
    ]


def refresh_requested() -> bool:
    return cache_root() is not None and os.environ.get("ATK_BOOTSTRAP_REFRESH") == "1"


def _download(root: Path, wheels: Path, *, project: bool) -> dict[str, Any]:
    requirements = ["-r", "agent_tools/requirements-mcp.txt"]
    if project:
        requirements += [
            *load_project(root / "pyproject.toml")["dependencies"],
            *PROJECT_TOOLS,
            *_build_requirements(root),
        ]
    return {
        "display": "Download/build dependency wheels",
        "network": True,
        "args": [
            str(root / ".venv/bin/python"),
            "-m",
            "pip",
            "wheel",
            "--wheel-dir",
            str(wheels),
            *requirements,
        ],
    }


def _install(
    root: Path,
    wheels: Path,
    *,
    project: bool,
    commands: list[tuple[str, dict[str, Any]]],
    execute: Callable[[dict[str, Any]], bool],
) -> bool:
    installations = [
        {**original, "env": {**original.get("env", {}), **_offline(wheels)}}
        for phase, original in commands
        if phase != "venv"
    ]
    return all(execute(command) for command in [*installations, *_health(root, project=project)])


def _populate(
    root: Path,
    directory: Path,
    *,
    project: bool,
    commands: list[tuple[str, dict[str, Any]]],
    execute: Callable[[dict[str, Any]], bool],
) -> None:
    wheels = directory / uuid.uuid4().hex
    wheels.mkdir(mode=0o700)
    published = False
    try:
        if not execute(_download(root, wheels, project=project)):
            return
        process.progress("Dependency cache: validating the new wheels with an offline install.")
        if not _install(root, wheels, project=project, commands=commands, execute=execute):
            return
        _publish(directory, wheels, directory.name)
        published = True
        _prune(directory, wheels)
    finally:
        if not published:
            shutil.rmtree(wheels)


def _prune(directory: Path, wheels: Path) -> None:
    for previous in directory.iterdir():
        if (
            previous != wheels
            and previous.is_dir()
            and len(previous.name) == GENERATION_LENGTH
            and all(char in "0123456789abcdef" for char in previous.name)
        ):
            shutil.rmtree(previous)


def prepare(
    root: Path,
    *,
    project: bool,
    commands: list[tuple[str, dict[str, Any]]],
    run: Callable[[Path, dict[str, Any]], dict[str, Any]] = process.run,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Publish a cache only after a full offline install and environment validation."""
    base = cache_root()
    if base is None:
        message = "Bootstrap cache directory was not supplied."
        raise RuntimeError(message)
    key = fingerprint(root, project=project)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = base / key
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    results: list[dict[str, Any]] = []
    cache = {"key": key, "reused": False}

    def execute(command: dict[str, Any]) -> bool:
        result = run(root, command)
        results.append(result)
        return bool(result["ok"])

    process.progress("Waiting for the dependency cache lock.")
    with process.lock(base / f"{key}.lock"):
        venv = next((command for phase, command in commands if phase == "venv"), None)
        if venv is None:
            # A cancelled ensurepip can leave the interpreter present but pip absent.
            # Re-running venv repairs its seed and configuration without clearing packages.
            venv = {
                "display": "Repair session virtual environment",
                "args": [sys.executable, "-m", "venv", str(root / ".venv")],
            }
        if not execute(venv):
            return results, cache
        wheels = None if refresh_requested() else _ready(directory, key)
        if wheels is None:
            _populate(root, directory, project=project, commands=commands, execute=execute)
        else:
            cache["reused"] = True
            process.progress("Dependency cache: reusing verified local wheels.")
            _install(root, wheels, project=project, commands=commands, execute=execute)
    return results, cache
