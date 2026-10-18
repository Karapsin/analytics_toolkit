"""Native failures retain inert diagnostics without admitting an active writer."""

from __future__ import annotations

import errno
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from tests.repository._support import native_probe as fixtures

if TYPE_CHECKING:
    from pathlib import Path

probe = fixtures.probe


def test_actual_install_program_preserves_completed_stages_when_pip_fails(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    work = tmp_path / "work"
    work.mkdir()

    def readonly(_value: str) -> None:
        raise OSError(errno.EROFS, "fixture read-only root")

    def path(value: str) -> Any:
        if value in {"/etc/atk-native-write-probe", "/trusted/.atk-native-write-probe"}:
            return SimpleNamespace(write_text=readonly)
        if value == "/proc/self/status":
            return SimpleNamespace(read_text=lambda: "CapEff: 0\nNoNewPrivs: 1\n")
        if value == "/var/run/docker.sock":
            return SimpleNamespace(exists=lambda: False)
        if value == "/work/probe-evidence":
            return work / "probe-evidence"
        return work if value == "/work" else tmp_path / "tmp-probe"

    calls: list[list[str]] = []

    def run(arguments: list[str], **_kwargs: Any) -> None:
        calls.append(arguments)
        if "pip" in arguments:
            raise subprocess.CalledProcessError(17, arguments)

    monkeypatch.setitem(sys.modules, "pathlib", SimpleNamespace(Path=path))
    monkeypatch.setitem(
        sys.modules,
        "subprocess",
        SimpleNamespace(
            run=run,
            check_output=lambda arguments, **_kwargs: (
                "git fixture" if "--version" in arguments else ""
            ),
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["install", json.dumps({"python": list(sys.version_info[:2])})]
    )
    monkeypatch.setattr(probe.os, "getuid", lambda: 1000)
    for name in [
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "GITHUB_OUTPUT",
        "GITHUB_ENV",
        "GITHUB_WORKSPACE",
        "RUNNER_TEMP",
    ]:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        exec(compile(probe.INSTALL, "contained-install-program", "exec"), {})
    assert raised.value.returncode == 17
    assert any("venv" in call for call in calls)
    report = json.loads((work / "probe-evidence/install.json").read_text())
    assert report["ok"] is False
    assert report["stage"] == "pip"
    assert report["completed"] == ["identity", "git", "venv"]
    assert report["git"] == "git fixture"
    assert report["error"]["type"] == "CalledProcessError"
    assert "contained-install-program" in report["error"]["traceback"]


@pytest.mark.parametrize(
    ("fault", "stage"),
    [
        ("black", "glyphs"),
        ("size", "geometry"),
        ("render", "render"),
        ("fonts", "fonts"),
        ("network", "network"),
        ("import", "imports"),
        ("success", "complete"),
    ],
)
def test_actual_capture_program_retains_original_failure_and_produced_png(
    tmp_path: Path,
    monkeypatch: Any,
    fault: str,
    stage: str,
) -> None:
    output, fonts, dimensions, png, calls = fixtures.configure_capture(tmp_path, monkeypatch, fault)
    if fault == "success":
        exec(compile(probe.CAPTURE, "contained-capture-program", "exec"), {})
    else:
        with pytest.raises((AssertionError, ValueError, ImportError)) as raised:
            exec(compile(probe.CAPTURE, "contained-capture-program", "exec"), {})
    report = json.loads((output / "capture.json").read_text())
    assert report["stage"] == stage
    assert report["ok"] is (fault == "success")
    assert report["visual_approval"] is False
    if fault != "success":
        assert report["error"]["type"] == type(raised.value).__name__
        assert report["error"]["message"] == str(raised.value)
        assert "contained-capture-program" in report["error"]["traceback"]
    if fault in {"black", "size", "success"}:
        assert (output / "infrastructure-only.png").read_bytes() == png
        assert report["png_sha256"] == hashlib.sha256(png).hexdigest()
        assert report["image_size"] == list(dimensions)
        assert report["rgb_extrema"] == ([[0, 0]] * 3 if fault == "black" else [[255, 255]] * 3)
    if calls:
        assert set(calls[0]) == {"svg_string"}
        assert (output / "infrastructure-only.svg").read_text() == calls[0]["svg_string"]
        assert report["render_options"] == {"svg_string": "infrastructure-only.svg"}
        assert (
            report["font_files"][str(fonts / "fixture.ttf")]
            == hashlib.sha256(b"unparseable inspection fixture").hexdigest()
        )
        assert report["font_metadata"][str(fonts / "fixture.ttf")]["inspection_error"]


@pytest.mark.parametrize(
    "fault",
    [
        "install",
        "capture",
        "capture-counter",
        "cleanup",
        "remaining",
        "uncertain",
        "writer",
        "missing",
        "counter",
        "success",
    ],
)
def test_native_main_collects_failed_evidence_only_after_certain_cleanup(
    tmp_path: Path,
    monkeypatch: Any,
    fault: str,
) -> None:
    state, command_file = fixtures.configure_main(tmp_path, monkeypatch, fault)
    if fault == "success":
        probe.main()
    else:
        with pytest.raises(SystemExit, match="Native infrastructure probe failed"):
            probe.main()
    output = next(tmp_path.glob("atk-native-probe-*"))
    report = json.loads((output / "isolation-diagnostics/native-probe.json").read_text())
    assert report["ok"] is (fault == "success")
    assert report["visual_approval"] is False
    assert state.counter_reads >= 2
    if fault in {"cleanup", "remaining", "uncertain", "writer"}:
        assert "collection_refused" in report
        assert not (output / "probe-evidence").exists()
    elif fault == "missing":
        assert report["collection_error"]
    else:
        assert report["evidence_collected"]
        assert (output / "probe-evidence/probe.json").is_file()
        if fault != "install":
            assert (output / "probe-evidence/capture.json").is_file()
    if fault == "counter":
        assert "counter response unavailable" in report["firewall_counter_error"]
        assert "counter response unavailable" in report["error"]
    if fault == "capture-counter":
        assert "counter response unavailable" in report["firewall_counter_error"]
        assert report["error"] == "ValueError: Native contained capture failed"
    if fault == "writer":
        assert command_file.read_bytes() == b""
    else:
        assert "artifact_root=" in command_file.read_text()
