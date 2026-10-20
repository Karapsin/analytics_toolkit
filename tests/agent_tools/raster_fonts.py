from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests.agent_tools._support import raster_fonts as fonts
from tests.agent_tools._support.raster_transport import identity

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("character", "covered"),
    [(" ", True), ("界", True), ("e", True), ("́", True), ("\U0010ffff", False)],
)
def test_observed_charset_includes_required_glyphs_without_claiming_renderer_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    character: str,
    covered: bool,
) -> None:
    path = tmp_path / "observed-font.ttc"
    path.write_bytes(b"exact observed font file")
    stdout = f"{fonts.SYNTHETIC_FAMILY}\n{path}\n0\n20-7e 300-36f 4e00-9fff".encode()
    calls = []

    def run(arguments: list[str], **kwargs: Any) -> Any:
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, stdout, b"")

    monkeypatch.setattr(fonts.subprocess, "run", run)
    record = fonts.observe_test_font(character)
    assert record["complete"] is covered
    assert record["character_coverage"][f"U+{ord(character):04X}"] is covered
    assert not record["missing_glyph_control_covered"]
    assert record["file"]["sha256"] == identity(path.read_bytes())["sha256"]
    assert record["file"]["byte_length"] == len(path.read_bytes())
    assert record["file"]["face_index"] == "0"
    assert "unobservable" in record["renderer_selection"]
    assert record["stdout"].encode() == stdout
    assert calls[0][0][-1] == fonts.SYNTHETIC_FAMILY
    assert calls[0][1]["timeout"] == 10


@pytest.mark.parametrize(
    "failure", ["fallback", "output-cap", "file-cap", "command", "missing-file"]
)
def test_font_fallback_and_bounded_failures_never_claim_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "font.ttc"
    path.write_bytes(b"file")
    family = "Unrelated fallback" if failure == "fallback" else fonts.SYNTHETIC_FAMILY
    stdout = f"{family}\n{path}\n0\n20-7e".encode()
    if failure == "missing-file":
        path.unlink()
    if failure == "output-cap":
        monkeypatch.setattr(fonts, "_MAX_PROBE_BYTES", 1)
    if failure == "file-cap":
        monkeypatch.setattr(fonts, "_MAX_FONT_BYTES", 1)

    def run(arguments: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(arguments, 1 if failure == "command" else 0, stdout, b"")

    monkeypatch.setattr(fonts.subprocess, "run", run)
    record = fonts.observe_test_font("e")
    assert not record["complete"]
