"""Exercise a new clone's real pip and editable build without any package index."""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from agent_tools import bootstrap_process
from tests.agent_tools._support.bootstrap import prepare, project

BACKEND = """
import pathlib
import zipfile

def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    name = "fixture_project-1.0-py3-none-any.whl"
    info = "fixture_project-1.0.dist-info/"
    with zipfile.ZipFile(pathlib.Path(wheel_directory) / name, "w") as wheel:
        wheel.writestr("fixture_project.pth", str(pathlib.Path.cwd()) + "\\n")
        wheel.writestr(info + "METADATA",
            "Metadata-Version: 2.1\\nName: fixture-project\\nVersion: 1.0\\n")
        wheel.writestr(info + "WHEEL", "Wheel-Version: 1.0\\nGenerator: fixture\\n"
            "Root-Is-Purelib: true\\nTag: py3-none-any\\n")
        wheel.writestr(info + "RECORD", "")
    return name
"""


def wheel(path: Path, name: str, modules: tuple[str, ...] = (), version: str = "1") -> None:
    info = name.replace("-", "_") + f"-{version}.dist-info/"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            info + "METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        )
        archive.writestr(
            info + "WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
        archive.writestr(info + "RECORD", "")
        for module in modules:
            archive.writestr(module + "/__init__.py", "")


def test_warm_cache_installs_and_imports_the_second_clone_without_pypi(tmp_path, monkeypatch):
    first = project(tmp_path / "first")
    (first / "backend.py").write_text(BACKEND)
    (first / "analytics_toolkit").mkdir()
    (first / "analytics_toolkit/__init__.py").write_text("")
    second = tmp_path / "second"
    shutil.copytree(first, second)
    monkeypatch.setenv("ATK_BOOTSTRAP_CACHE_DIR", str(tmp_path / "cache with spaces"))
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_INDEX_URL", "http://127.0.0.1:9/unavailable")
    downloads = []

    def runner(root, command):
        if command.get("network"):
            downloads.append(command)
            destination = Path(command["args"][command["args"].index("--wheel-dir") + 1])
            wheel(
                destination / "fixture_runtime-1-py3-none-any.whl",
                "fixture-runtime",
                ("mcp", "pytest", "tox"),
            )
            wheel(destination / "wheel-1-py3-none-any.whl", "wheel")
            wheel(destination / "pytest-1-py3-none-any.whl", "pytest")
            wheel(destination / "tox-1-py3-none-any.whl", "tox")
            wheel(destination / "virtualenv-21.1-py3-none-any.whl", "virtualenv", version="21.1")
            return {"ok": True, "command": command["display"], "returncode": 0}
        return bootstrap_process.run(root, command)

    cold, _ = prepare(first, runner)
    # Reproduce a preparation interrupted between interpreter creation and ensurepip.
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(second / ".venv")],
        check=True,
    )
    warm, status = prepare(second, runner)
    assert all(item["ok"] for item in [*cold, *warm]), [*cold, *warm]
    assert status["reused"] is True
    assert len(downloads) == 1
    output = subprocess.run(
        [
            str(second / ".venv/bin/python"),
            "-c",
            "import analytics_toolkit; print(analytics_toolkit.__file__)",
        ],
        cwd=second,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert Path(output).resolve() == second / "analytics_toolkit/__init__.py"
