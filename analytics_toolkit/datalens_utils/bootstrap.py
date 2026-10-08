"""Optional, explicit project bootstrap; importing it performs no setup or login."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import DataLensConfigurationError
from .session import ProjectPaths
from .settings import platform_key


def default_runtime_root(project_root: Any) -> Any:
    """Keep managed tools and checkpoints outside the shareable recipe."""
    root = Path(project_root).resolve()
    return (
        Path(
            os.environ.get(
                "DATALENS_RUNTIME_DIR", root.parent / ".local" / root.name.replace(" ", "")
            )
        )
        .expanduser()
        .resolve()
    )


def managed_yc_binary(runtime_root: Any) -> Any:
    return (
        Path(runtime_root)
        / ".tools"
        / platform_key()
        / "bin"
        / ("yc.exe" if sys.platform == "win32" else "yc")
    )


def ensure_environment(
    project_root: Any,
    runtime_root: Any,
    *,
    argv: Any = (),
    organization_id: Any = "",
    yc_profile: Any = "",
) -> Any:
    """Resolve the project's pinned runtime, bootstrapping and relaunching if needed.

    This is a launcher operation, not an implicit side effect of engine methods.
    Tools and environments are project-owned; shell profiles and login remain
    outside this function. The namespace is computed for future toolkit imports.
    """
    paths = ProjectPaths(project_root, runtime_root)
    native = platform_key()
    windows = native.startswith("windows-")
    python = paths.runtime_root / ".venv" / ("Scripts/python.exe" if windows else "bin/python")
    tool_root = paths.runtime_root / ".tools" / native
    expected = tuple(
        int(value)
        for value in (paths.project_root / ".python-version").read_text().strip().split(".")
    )
    probe = (
        "import importlib, sys; from pathlib import Path; from importlib.metadata import version; "
        "assert tuple(sys.version_info[:3]) == tuple(map(int, sys.argv[2].split('.'))); "
        "assert version('datalens-sdk') == '3.1.0'; importlib.import_module(sys.argv[3]); "
        "assert Path(sys.base_prefix).resolve().is_relative_to(Path(sys.argv[1]).resolve())"
    )
    ready = False
    if python.is_file() and managed_yc_binary(paths.runtime_root).is_file():
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            ready = (
                subprocess.run(  # noqa: S603
                    [
                        str(python),
                        "-B",
                        "-c",
                        probe,
                        str(tool_root / "python"),
                        ".".join(str(value) for value in expected),
                        __package__,
                    ],
                    capture_output=True,
                    timeout=30,
                    check=False,
                ).returncode
                == 0
            )
    if not ready:
        print(f"Setting up local tools for {native}…", flush=True)
        script = (
            Path(__file__).resolve().parent
            / "bootstrap_assets"
            / ("bootstrap.ps1" if windows else "bootstrap.sh")
        )
        command = (
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                str(paths.project_root),
            ]
            if windows
            else ["bash", str(script), str(paths.project_root)]
        )
        environment = {
            **os.environ,
            "DATALENS_RUNTIME_DIR": str(paths.runtime_root),
            "DATALENS_BOOTSTRAP_MODULE": __name__,
            "DATALENS_ORG_ID": organization_id,
            "DATALENS_YC_PROFILE": yc_profile,
        }
        environment.pop("VIRTUAL_ENV", None)
        completed = subprocess.run(command, cwd=paths.project_root, env=environment, check=False)  # noqa: S603
        if completed.returncode:
            raise SystemExit(completed.returncode)
    if not ready or Path(sys.prefix).resolve() != paths.runtime_root / ".venv":
        raise SystemExit(
            subprocess.run(  # noqa: S603
                [str(python), "-B", str(paths.project_root / "dashboard.py"), *argv],
                cwd=paths.project_root,
                check=False,
            ).returncode
        )
    return python


def check_environment(project_root: Any, runtime_root: Any) -> Any:
    """Check installed runtime and bundled SDK configuration without cloud access."""

    from importlib.metadata import version  # noqa: PLC0415

    import datalens_sdk  # noqa: PLC0415

    print("Python", sys.version.split()[0])
    print("DataLens SDK", version("datalens-sdk"))

    paths = ProjectPaths(project_root, runtime_root)
    for source in paths.project_root.rglob("*.py"):
        if "__pycache__" not in source.parts:
            compile(source.read_text(encoding="utf-8"), str(source), "exec")
    binary = managed_yc_binary(paths.runtime_root)
    if not binary.is_file():
        message = "Managed yc CLI is missing."
        raise DataLensConfigurationError(message)
    if sys.platform == "win32":
        print("YC configuration prerequisites detected; cloud login is checked at runtime.")
        return
    skill = Path(datalens_sdk.agent_skill_paths()[0])
    completed = subprocess.run(  # noqa: S603
        ["bash", str(skill / "scripts/preflight.sh"), "yc"],  # noqa: S607
        cwd=paths.project_root,
        env={
            **os.environ,
            "PYTHON": sys.executable,
            "DATALENS_INSTALLATION": "yc",
            "DATALENS_YC_BIN": str(binary),
        },
        text=True,
        capture_output=True,
        check=True,
    )
    print(completed.stdout, end="")
    if (
        "---PREFLIGHT---" not in completed.stdout
        or "STATUS=ready" not in completed.stdout.splitlines()
    ):
        message = "Bundled SDK preflight is not ready."
        raise DataLensConfigurationError(message)
    print("Local setup checks passed; no cloud or data request was made.")
