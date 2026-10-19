"""Disposable GitHub-only renderer compatibility observations, never adoption."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import struct
import subprocess
import sys
import tempfile
import traceback
import venv
from email.parser import BytesParser
from importlib.metadata import metadata, version
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from tests._support.paths import REPO_ROOT
from tests.agent_tools._support.raster_transport import (
    bound_artifacts,
    source_binding,
    write_report,
)

MAX_COMMAND_BYTES = 1024 * 1024
COMMAND_TIMEOUT = 300
RENDERER_REQUIREMENT = "resvg-py==0.3.2"


def command(arguments: list[str], output: Path, name: str) -> dict[str, Any]:
    """Retain original stdout/stderr and exit codes, including unsuccessful commands."""
    record: dict[str, Any] = {"arguments": arguments, "complete": True}
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(
                arguments,
                cwd=REPO_ROOT,
                stdout=stdout,
                stderr=stderr,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
            record["exit_code"] = result.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            record.update(
                exit_code=None, complete=False, failure=type(exc).__name__, message=str(exc)
            )
        for kind, stream in (("stdout", stdout), ("stderr", stderr)):
            length = stream.tell()
            stream.seek(0)
            data = stream.read(MAX_COMMAND_BYTES + 1)
            observation: dict[str, Any] = {"byte_length": length}
            if length > MAX_COMMAND_BYTES:
                record["complete"] = False
                observation["omission"] = "command_output_byte_limit"
            else:
                path = output / (name + "." + kind)
                path.write_bytes(data)
                observation.update(path=path.name, sha256=hashlib.sha256(data).hexdigest())
            record[kind] = observation
    return record


def wheel_identity(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    with ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            message = "ambiguous_wheel_metadata"
            raise ValueError(message)
        raw = archive.read(names[0])
    parsed = BytesParser().parsebytes(raw)
    return {
        "path": path.name,
        "byte_length": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "metadata": {
            "encoding": "UTF-8",
            "content": raw.decode("utf-8"),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_length": len(raw),
        },
        "name": parsed["Name"],
        "version": parsed["Version"],
        "requires_python": parsed["Requires-Python"],
    }


def inspect_installed(output: Path) -> None:
    renderer = importlib.import_module("resvg_py")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2">'
        '<path d="M0 0h2v2H0"/></svg>'
    )
    png = bytes(renderer.svg_to_bytes(svg_string=svg))
    path = output / "api.png"
    path.write_bytes(png)
    dimensions = list(struct.unpack_from(">II", png, 16))
    if png[:8] != b"\x89PNG\r\n\x1a\n" or dimensions != [2, 2]:
        message = "unexpected_renderer_api_png"
        raise ValueError(message)
    report = {
        "schema_version": 1,
        "complete": True,
        "binding": source_binding(),
        "version": version("resvg-py"),
        "requires_python": metadata("resvg-py").get("Requires-Python"),
        "api": "resvg_py.svg_to_bytes(svg_string=exact SVG)",
        "svg": svg,
        "png": {
            "path": path.name,
            "byte_length": len(png),
            "sha256": hashlib.sha256(png).hexdigest(),
            "dimensions": dimensions,
        },
    }
    write_report(output / "installed-api.json", report)


def finish_probe(output: Path, report: dict[str, Any]) -> int:
    report["omissions"] = bound_artifacts(output)
    report["complete"] = report["complete"] and not report["omissions"]
    complete = write_report(output / "probe.json", report)
    return 0 if complete else 1


def probe(output: Path, profile: str) -> int:
    output.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": 1,
        "complete": False,
        "binding": source_binding(),
        "requirement": RENDERER_REQUIREMENT,
        "profile": profile,
        "commands": [],
        "qualification": "Disposable probe only; ordinary requirements/jobs stay unchanged",
    }
    write_report(output / "probe.json", report)
    with tempfile.TemporaryDirectory(prefix="renderer-compatibility-") as temporary:
        environment = Path(temporary) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = str(environment / "bin/python")
        downloads = output / "distribution"
        downloads.mkdir()
        commands = [
            ("pip", [python, "-m", "pip", "install", "--upgrade", "pip"]),
            (
                "download",
                [
                    python,
                    "-m",
                    "pip",
                    "download",
                    "--no-deps",
                    "--only-binary=:all:",
                    "--dest",
                    str(downloads),
                    RENDERER_REQUIREMENT,
                ],
            ),
        ]
        for name, arguments in commands:
            observed = command(arguments, output, name)
            report["commands"].append(observed)
            write_report(output / "probe.json", report)
            if observed["exit_code"] != 0:
                return finish_probe(output, report)
        wheels = sorted(downloads.glob("*.whl"))
        if len(wheels) != 1:
            report["missing"] = ["one downloaded renderer wheel required"]
            return finish_probe(output, report)
        report["distribution"] = wheel_identity(wheels[0])
        report["distribution"]["path"] = str(wheels[0].relative_to(output))
        install = [python, "-m", "pip", "install", "--report", str(output / "install.json")]
        if profile == "py39-min":
            install.extend(["-c", "constraints/py39-min.txt"])
        install.extend(
            [
                "-e",
                ".[tui,datalens]",
                "pytest==8.3.5" if profile == "py39-min" else "pytest",
                "Pillow>=10,<13",
                str(wheels[0]),
            ]
        )
        observed = command(install, output, "install")
        report["commands"].append(observed)
        if observed["exit_code"] == 0:
            executions = [
                (
                    "api",
                    [
                        python,
                        "-m",
                        "tests.agent_tools._support.renderer_probe",
                        "--inspect-installed",
                        "--output",
                        str(output),
                    ],
                ),
                (
                    "observations",
                    [
                        python,
                        "-m",
                        "tests.agent_tools._support.raster_observations",
                        "--output",
                        str(output / "raster.json"),
                    ],
                ),
                (
                    "strict-cases",
                    [python, "-m", "pytest", "-q", "tests/agent_tools/underline_rasterization.py"],
                ),
                ("pip-check", [python, "-m", "pip", "check"]),
                ("freeze", [python, "-m", "pip", "freeze", "--all"]),
            ]
            for name, arguments in executions:
                report["commands"].append(command(arguments, output, name))
                write_report(output / "probe.json", report)
        report["complete"] = all(
            item["complete"] and item["exit_code"] == 0 for item in report["commands"]
        )
    return finish_probe(output, report)


def collect_current(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    observed = command(
        [
            sys.executable,
            "-m",
            "tests.agent_tools._support.raster_observations",
            "--output",
            str(output / "raster.json"),
        ],
        output,
        "observations",
    )
    omissions = bound_artifacts(output)
    complete = observed["complete"] and observed["exit_code"] == 0 and not omissions
    retained = write_report(
        output / "invocation.json",
        {
            "schema_version": 1,
            "complete": complete,
            "binding": source_binding(),
            "command": observed,
            "omissions": omissions,
        },
    )
    return 0 if complete and retained else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=("py39-min", "py39-latest"), default="py39-latest")
    parser.add_argument("--inspect-installed", action="store_true")
    parser.add_argument("--collect-current", action="store_true")
    arguments = parser.parse_args()
    arguments.output.mkdir(parents=True, exist_ok=True)
    if arguments.collect_current:
        return collect_current(arguments.output.resolve())
    if arguments.inspect_installed:
        inspect_installed(arguments.output.resolve())
        return 0
    try:
        return probe(arguments.output.resolve(), arguments.profile)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        failure = traceback.format_exc()
        omissions = bound_artifacts(arguments.output)
        write_report(
            arguments.output / "fatal.json",
            {
                "schema_version": 1,
                "complete": False,
                "binding": source_binding(),
                "traceback": failure,
                "omissions": omissions,
            },
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
