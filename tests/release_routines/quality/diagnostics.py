from __future__ import annotations

import hashlib
import json
import subprocess
from typing import TYPE_CHECKING

import pytest
from release_routines.lib import quality_debt, quality_diagnostics

from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


@pytest.fixture
def diagnostic_identity(monkeypatch: Any) -> None:
    monkeypatch.setenv("ATK_SOURCE_HEAD", "a" * 40)
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    monkeypatch.setattr(quality_diagnostics, "checkout_revision", lambda root: "c" * 40)
    monkeypatch.setattr(quality_diagnostics, "toolchain", lambda: {"mypy": "1.16.1"})


@pytest.mark.usefixtures("diagnostic_identity")
@pytest.mark.parametrize("group", ["lint", "type"])
def test_full_new_diagnostic_survives_debt_aggregation(tmp_path: Path, group: str) -> None:
    baseline = tmp_path / "baseline.json"
    quality_debt.write_baseline(baseline, {"ruff": {}, "ruff_format": {}, "mypy": {}})
    message = "Precise diagnostic " + "details " * 10000 + "LAST DETAIL"
    row = {
        "filename": "editor.py",
        "file": "editor.py",
        "code": "assignment",
        "severity": "error",
        "message": message,
        "line": 98,
        "column": 29,
    }
    stdout = json.dumps([row] if group == "lint" else row)

    def runner(command: Any, cwd: Path) -> quality_debt.CommandResult:
        if command[1] == "format":
            return quality_debt.CommandResult(0, "All formatted", "")
        return quality_debt.CommandResult(1, stdout, "Full stderr " + message)

    result = quality_debt.run_gate(
        group,
        root=tmp_path,
        baseline_path=baseline,
        write=False,
        runner=runner,
    )
    assert result == 1
    indexes = list((tmp_path / ".rag_index/quality-diagnostics").glob("*/index.json"))
    assert len(indexes) == (2 if group == "lint" else 1)
    index = next(
        path
        for path in indexes
        if "LAST DETAIL" in (path.parent / "stdout.txt").read_text(encoding="utf-8")
    )
    record = json.loads(index.read_text(encoding="utf-8"))
    assert record["checkout_revision"] == "c" * 40
    assert record["source_head"] == "a" * 40
    assert record["workflow_sha"] == "b" * 40
    assert record["toolchain"] == {"mypy": "1.16.1"}
    assert record["returncode"] == 1
    if group == "type":
        assert record["command"][-2:] == ["analytics_toolkit", "atk"]
    else:
        assert record["command"][:4] == ["ruff", "check", "--output-format", "json"]
    for name, expected in (("stdout", stdout), ("stderr", "Full stderr " + message)):
        payload = (index.parent / record["outputs"][name]["path"]).read_bytes()
        assert payload == expected.encode("utf-8")
        assert record["outputs"][name]["bytes"] == len(payload)
        assert record["outputs"][name]["sha256"] == hashlib.sha256(payload).hexdigest()


@pytest.mark.usefixtures("diagnostic_identity")
@pytest.mark.parametrize("group", ["lint", "type"])
@pytest.mark.parametrize("failure", ["parse", "tool", "spawn"])
def test_failure_retains_complete_output_before_validation(
    tmp_path: Path, monkeypatch: Any, group: str, failure: str
) -> None:
    monkeypatch.setattr(quality_debt, "PROJECT_ROOT", tmp_path)
    raw = "invalid JSON " + "x" * 50000
    stderr = "configuration error " + "y" * 50000

    def runner(command: Any, cwd: Path) -> quality_debt.CommandResult:
        if failure == "spawn":
            raise OSError(stderr)
        return quality_debt.CommandResult(2 if failure == "tool" else 1, raw, stderr)

    if failure == "spawn":
        with pytest.raises(OSError, match="configuration error"):
            quality_debt.main([group], runner=runner)
    else:
        assert quality_debt.main([group], runner=runner) == 2
    (index,) = (tmp_path / ".rag_index/quality-diagnostics").glob("*/index.json")
    record = json.loads(index.read_text(encoding="utf-8"))
    expected_code = {"spawn": None, "tool": 2, "parse": 1}[failure]
    assert record["returncode"] == expected_code
    assert (index.parent / "stdout.txt").read_text(encoding="utf-8") == (
        "" if failure == "spawn" else raw
    )
    assert (index.parent / "stderr.txt").read_text(encoding="utf-8") == stderr


@pytest.mark.parametrize("missing_name", ["ruff", "mypy", "textual", "rich"])
def test_toolchain_preserves_mixed_installed_and_missing_tools(
    monkeypatch: Any, missing_name: str
) -> None:
    expected = {"ruff": "0.15.22", "mypy": "1.16.1", "textual": "0.73.0", "rich": "13.9.4"}

    def version(name: str) -> str:
        if name == missing_name:
            raise quality_diagnostics.metadata.PackageNotFoundError(name)
        return expected[name]

    monkeypatch.setattr(quality_diagnostics.metadata, "version", version)
    monkeypatch.setattr(quality_diagnostics.platform, "python_version", lambda: "3.11.17")
    assert quality_diagnostics.toolchain() == {
        "python": "3.11.17",
        **expected,
        missing_name: None,
    }


def test_toolchain_propagates_unrelated_metadata_errors(monkeypatch: Any) -> None:
    error = RuntimeError("Invalid distribution metadata")

    def broken(name: str) -> str:
        raise error

    monkeypatch.setattr(quality_diagnostics.metadata, "version", broken)
    with pytest.raises(RuntimeError, match="Invalid distribution metadata") as caught:
        quality_diagnostics.toolchain()
    assert caught.value is error


def test_metadata_records_missing_tools_and_checkout(tmp_path: Path, monkeypatch: Any) -> None:
    def missing(name: str) -> str:
        raise quality_diagnostics.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(quality_diagnostics.metadata, "version", missing)
    versions = quality_diagnostics.toolchain()
    assert versions["python"]
    assert versions["ruff"] is None
    monkeypatch.setattr(
        quality_diagnostics.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", ""),
    )
    assert quality_diagnostics.checkout_revision(tmp_path) is None

    def absent(*args: Any, **kwargs: Any) -> None:
        message = "git unavailable"
        raise FileNotFoundError(message)

    monkeypatch.setattr(quality_diagnostics.subprocess, "run", absent)
    assert quality_diagnostics.checkout_revision(tmp_path) is None


def test_quality_workflow_always_uploads_diagnostics_and_passes_source_identity() -> None:
    workflow = (REPO_ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    quality = workflow.split("  quality:\n", 1)[1]
    retention = quality.split("- name: Retain complete quality tool diagnostics\n", 1)[1]
    assert retention.startswith("        if: always()\n")
    assert "path: .rag_index/quality-diagnostics/" in retention
    assert "include-hidden-files: true" in retention
    assert "github.event.pull_request.head.sha || github.sha" in quality
    tox = (REPO_ROOT / "tox.ini").read_text(encoding="utf-8")
    passed = tox.split("pass_env =\n", 1)[1].split("deps =", 1)[0]
    for key in (
        "ATK_SOURCE_HEAD",
        "GITHUB_SHA",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_JOB",
    ):
        assert key in passed


@pytest.mark.usefixtures("diagnostic_identity")
def test_formatter_failure_retains_both_streams_with_a_bounded_summary(tmp_path: Path) -> None:
    raw = "full formatter output " + "x" * 50000
    stderr = "formatter failed " + "y" * 50000

    def runner(command: Any, cwd: Path) -> quality_debt.CommandResult:
        if command[1] == "check":
            return quality_debt.CommandResult(0, "[]", "")
        return quality_debt.CommandResult(2, raw, stderr)

    with pytest.raises(quality_debt.QualityDebtError, match="Ruff format") as caught:
        quality_debt.collect_ruff_findings(tmp_path, runner)
    assert len(str(caught.value)) < 2100
    indexes = list((tmp_path / ".rag_index/quality-diagnostics").glob("*/index.json"))
    assert len(indexes) == 2
    records = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in indexes]
    path, record = next((path, item) for path, item in records if item["returncode"] == 2)
    assert record["command"][:3] == ["ruff", "format", "--check"]
    assert (path.parent / "stdout.txt").read_text(encoding="utf-8") == raw
    assert (path.parent / "stderr.txt").read_text(encoding="utf-8") == stderr
