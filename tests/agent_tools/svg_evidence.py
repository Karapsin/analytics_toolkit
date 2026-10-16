from __future__ import annotations

import base64
import json
import struct
import subprocess
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agent_tools import sql_explorer_svg_evidence as evidence
from agent_tools import sql_explorer_visual_capture as capture

if TYPE_CHECKING:
    from pathlib import Path

SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>界e\u0301 .connections</text></svg>'


@pytest.fixture(autouse=True)
def no_font_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence.shutil, "which", lambda _name: None)


@pytest.mark.parametrize("extra", [0, 1])
def test_lossless_utf8_boundary_never_truncates_content(
    monkeypatch: pytest.MonkeyPatch, extra: int
) -> None:
    frame = f"<svg>{SVG}</svg>"
    total = len(SVG.encode("utf-8")) + len(frame.encode("utf-8"))
    monkeypatch.setattr(evidence, "MAX_SVG_BYTES", total - extra)
    result = json.loads(json.dumps(evidence.export_input_evidence(SVG, frame, b"png")))
    assert result["complete"] is (extra == 0)
    assert result["combined_svg_byte_length"] == total
    assert result["missing"] == ([] if extra == 0 else ["combined_svg_byte_limit"])
    for name, original in (("raw_svg", SVG), ("framed_svg", frame)):
        record = result["inputs"][name]
        assert record["encoding"] == "UTF-8"
        assert record["byte_length"] == len(original.encode("utf-8"))
        assert record["sha256"] == evidence.digest(original.encode("utf-8"))
        if extra:
            assert "content" not in record
        else:
            assert record["content"].encode("utf-8") == original.encode("utf-8")
    assert result["png_sha256"] == evidence.digest(b"png")


def test_versions_and_unavailable_font_probes_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_package: str) -> str:
        raise evidence.PackageNotFoundError

    monkeypatch.setattr(evidence, "version", missing)
    svg = (
        '<svg font-family="Monospace"><style>.a {font-family: Fira Code, monospace;}</style>'
        '<text style="font-family: Courier">heading</text></svg>'
    )
    result = evidence.export_input_evidence(svg, svg, b"png")
    assert result["versions"]["python"]
    assert all(result["versions"][key] == "unavailable" for key in ("textual", "rich", "resvg-py"))
    fonts = result["fonts"]
    assert fonts["requested_families"] == ["Courier", "Fira Code, monospace", "Monospace"]
    assert fonts["available_fonts"]["status"] == "unavailable"
    assert fonts["available_fonts"]["command"][0] == "fc-list"
    assert all(match["probe"]["status"] == "unavailable" for match in fonts["system_matches"])
    assert fonts["renderer_selection"]["status"] == "unobservable"
    assert result["complete"] is True  # Missing probes never masquerade as renderer observations.


@pytest.mark.parametrize("outcome", ["ok", "failed", "timeout", "oserror"])
def test_font_probes_bound_output_and_record_failure(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.setattr(evidence.shutil, "which", lambda _name: "/fake/fc-list")

    def run(arguments: list[str], **kwargs: Any) -> Any:
        assert arguments == ["/fake/fc-list"]
        assert kwargs["timeout"] == evidence.PROBE_TIMEOUT
        assert kwargs["check"] is False
        kwargs["stdout"].write(b"x" * (evidence.MAX_PROBE_BYTES + 1))
        kwargs["stderr"].write(b"diagnostic")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(arguments, evidence.PROBE_TIMEOUT)
        if outcome == "oserror":
            raise OSError
        return SimpleNamespace(returncode=0 if outcome == "ok" else 1)

    monkeypatch.setattr(evidence.subprocess, "run", run)
    result = evidence._font_probe(["fc-list"])
    assert result["status"] == ("unavailable" if outcome in {"timeout", "oserror"} else outcome)
    assert result["stdout"]["truncated"] is True
    assert result["stdout"]["byte_length"] == evidence.MAX_PROBE_BYTES + 1
    assert len(result["stdout"]["text"]) == evidence.MAX_PROBE_BYTES
    assert result["stderr"]["text"] == "diagnostic"
    assert result["stderr"]["truncated"] is False


def test_font_requests_are_bounded_without_claiming_renderer_selection() -> None:
    svg = (
        "<svg>"
        + "".join(f'<text font-family="family-{index}">x</text>' for index in range(20))
        + "</svg>"
    )
    result = evidence.export_input_evidence(svg, svg, b"png")["fonts"]
    assert result["requested_family_count"] == 20
    assert len(result["requested_families"]) == evidence.MAX_FONT_REQUESTS
    assert len(result["system_matches"]) == evidence.MAX_FONT_REQUESTS
    assert result["requests_truncated"] is True
    assert result["renderer_selection"]["status"] == "unobservable"


def test_long_font_name_is_recorded_as_unavailable_without_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def probe(arguments: list[str]) -> dict[str, str]:
        calls.append(arguments)
        return {"status": "unavailable"}

    monkeypatch.setattr(evidence, "_font_probe", probe)
    svg = f'<svg font-family="{"a" * 513}"/>'
    fonts = evidence.export_input_evidence(svg, svg, b"png")["fonts"]
    assert fonts["family_names_truncated"] is True
    assert len(fonts["requested_families"][0]) == 512
    assert fonts["system_matches"][0]["probe"]["reason"] == "font_family_length_limit"
    assert len(calls) == 1  # Inventory only; no oversized command argument.


def test_export_inputs_survive_incomplete_parsed_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(evidence, "_render_svg", lambda _svg: b"png")

    geometry = tmp_path / "geometry.json"
    png = tmp_path / "scene.png"
    monkeypatch.setattr(capture, "_scene_ready", lambda _app: True)
    monkeypatch.setattr(
        capture,
        "_write_evidence",
        lambda *_args: geometry.write_text('{"ok": true, "assertions": {}}'),
    )
    monkeypatch.setattr(capture, "terminal_diagnostics", lambda *_args: {})
    monkeypatch.setattr(
        capture,
        "render_diagnostics",
        lambda *_args: {"complete": False, "missing": ["payload_exceeds_byte_limit"]},
    )
    renderer_inputs = []

    def render(_svg: str, path: Any, *, framed_svg: str) -> None:
        renderer_inputs.append(framed_svg)
        path.write_bytes(b"png")

    monkeypatch.setattr(capture, "_write_png", render)
    app = SimpleNamespace(export_screenshot=lambda **_kwargs: SVG)
    capture._capture_frame(app, "database-picker", geometry, tmp_path / "manifest", png)
    saved = json.loads(geometry.read_text())
    assert saved["ok"] is True
    assert saved["render_diagnostics"]["complete"] is False
    assert saved["export_input_evidence"]["complete"] is True
    assert saved["export_input_evidence"]["inputs"]["framed_svg"]["content"] == renderer_inputs[0]

    monkeypatch.setattr(evidence, "MAX_SVG_BYTES", 1)
    capture._capture_frame(app, "database-picker", geometry, tmp_path / "manifest", png)
    saved = json.loads(geometry.read_text())
    assert saved["export_input_evidence"]["complete"] is False
    assert "content" not in saved["export_input_evidence"]["inputs"]["raw_svg"]
    assert saved["render_diagnostics"]["complete"] is False


COMPARISON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2556 1196.8">'
    '<style>@font-face {font-family: "Fira Code";}'
    ".terminal-123-matrix {font-family: Fira Code, monospace; font-size: 20px;}"
    ".terminal-123-title {font-family: arial;}</style>"
    '<g class="terminal-123-matrix" transform="translate(9,22)" clip-path="url(#clip)">'
    '<text x="20" y="40">Select a .connections database key \u754ce\u0301</text></g></svg>'
)
FONT_VALUE = "'DejaVu Sans Mono', monospace"
# A complete, losslessly retained PNG; no renderer is needed by these unit tests.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


@pytest.fixture
def comparison_renderer(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def probe(_arguments: list[str]) -> dict[str, Any]:
        return {
            "status": "ok",
            "stdout": {"text": "DejaVu Sans Mono\t/fonts/mono.ttf\n", "truncated": False},
        }

    def render(svg: str) -> bytes:
        calls.append(svg)
        return PNG

    monkeypatch.setattr(evidence, "_font_probe", probe)
    monkeypatch.setattr(evidence, "_render_svg", render)
    return calls


def test_four_variants_preserve_inputs_hashes_and_ordinary_png(
    comparison_renderer: list[str],
) -> None:
    framed = capture._frame_svg(COMPARISON_SVG)
    raw_font = COMPARISON_SVG.replace("Fira Code, monospace", FONT_VALUE)
    framed_font = framed.replace("Fira Code, monospace", FONT_VALUE)
    result = json.loads(json.dumps(evidence.rasterization_comparisons(COMPARISON_SVG, framed, PNG)))
    assert result["schema_version"] == 1
    assert result["complete"] is True
    assert result["missing"] == []
    assert comparison_renderer == [COMPARISON_SVG, raw_font, framed_font]
    expected = {
        "raw_original": COMPARISON_SVG,
        "raw_dejavu": raw_font,
        "framed_original": framed,
        "framed_dejavu": framed_font,
    }
    assert set(result["variants"]) == set(expected)
    assert result["combined_svg_byte_length"] == sum(
        len(original.encode("utf-8")) for original in expected.values()
    )
    assert result["combined_png_byte_length"] == len(PNG) * len(expected)
    for identity, original in expected.items():
        record = result["variants"][identity]
        assert record["status"] == "ok"
        assert record["svg"]["content"] == original
        encoded = original.encode("utf-8")
        assert record["svg"]["byte_length"] == len(encoded)
        assert record["svg"]["sha256"] == evidence.digest(encoded)
        png = base64.b64decode(record["png"]["content"], validate=True)
        assert png == PNG
        assert record["png"]["byte_length"] == len(png)
        assert record["png"]["sha256"] == evidence.digest(png)
        assert record["png"]["dimensions"] == [1, 1]
        assert record["png"]["reused_ordinary_png"] is (identity == "framed_original")
        assert record["renderer_arguments"] == {"input": "svg.content", "other_arguments": {}}
    assert result["ordinary_png_sha256"] == evidence.digest(PNG)
    assert result["versions"]["python"]
    assert "unobservable" in result["comparison_font"]["renderer_selection"]


@pytest.mark.parametrize("framed", [False, True])
def test_font_change_preserves_heading_title_layout_and_all_other_bytes(framed: bool) -> None:
    original = capture._frame_svg(COMPARISON_SVG) if framed else COMPARISON_SVG
    changed = evidence._terminal_font_variant(original)
    assert changed == original.replace("Fira Code, monospace", FONT_VALUE)
    assert ".connections" in changed
    assert "font-family: arial;" in changed
    assert 'font-family: "Fira Code";' in changed


@pytest.mark.parametrize(
    ("css", "reason"),
    [
        (".title {font-family: arial;}", "terminal_matrix"),
        (".terminal-1-matrix {} .terminal-2-matrix {}", "terminal_matrix"),
        (".terminal-1-matrix, .title {font-family: x;}", "selector"),
        (".terminal-1-matrix:hover {font-family: x;}", "selector"),
        (".terminal-1-matrix {font-size: 20px;}", "terminal_font"),
        (".terminal-1-matrix {font-family: x; font-family: y;}", "terminal_font"),
    ],
)
def test_ambiguous_font_inputs_are_explicitly_incomplete(
    css: str, reason: str, comparison_renderer: list[str]
) -> None:
    svg = f"<svg><style>{css}</style><text>.connections</text></svg>"
    result = evidence.rasterization_comparisons(svg, capture._frame_svg(svg), PNG)
    assert result["complete"] is False
    for identity in ("raw_dejavu", "framed_dejavu"):
        record = result["variants"][identity]
        assert record["status"] == "omitted"
        assert reason in record["reason"]
        assert "svg" not in record
    assert comparison_renderer == [svg]


@pytest.mark.parametrize(
    "observation",
    [
        ("unavailable", "", False, False),
        ("failed", "DejaVu Sans Mono\t/font.ttf", False, False),
        ("ok", "Liberation Mono\t/font.ttf", False, False),
        ("ok", "DejaVu Sans Mono\t/font.ttf", True, False),
        ("ok", "DejaVu Sans Mono", False, False),
        ("ok", "DejaVu Sans Mono\t", False, False),
        ("ok", "DejaVu Sans Mono\t/a\nDejaVu Sans Mono\t/b", False, False),
        ("ok", "Other Alias,DejaVu Sans Mono\t/font.ttf", False, True),
    ],
)
def test_font_availability_requires_exact_nontruncated_installed_family(
    monkeypatch: pytest.MonkeyPatch,
    comparison_renderer: list[str],
    observation: tuple[str, str, bool, bool],
) -> None:
    status, text, truncated, available = observation

    def probe(arguments: list[str]) -> dict[str, Any]:
        assert arguments == [
            "fc-match",
            "--format=%{family}\t%{file}\n",
            "--",
            "DejaVu Sans Mono",
        ]
        return {"status": status, "stdout": {"text": text, "truncated": truncated}}

    monkeypatch.setattr(evidence, "_font_probe", probe)
    result = evidence.rasterization_comparisons(
        COMPARISON_SVG, capture._frame_svg(COMPARISON_SVG), PNG
    )
    assert result["comparison_font"]["available"] is available
    assert result["complete"] is available
    if not available:
        assert comparison_renderer == [COMPARISON_SVG]
        assert result["variants"]["raw_dejavu"]["reason"] == "comparison_font_unavailable"


@pytest.mark.parametrize("kind", ["svg", "png"])
@pytest.mark.parametrize("extra", [0, 1])
def test_combined_byte_limits_keep_exact_boundary_and_omit_without_truncation(
    monkeypatch: pytest.MonkeyPatch, comparison_renderer: list[str], kind: str, extra: int
) -> None:
    framed = capture._frame_svg(COMPARISON_SVG)
    baseline = evidence.rasterization_comparisons(COMPARISON_SVG, framed, PNG)
    total = baseline[f"combined_{kind}_byte_length"]
    constant = "MAX_SVG_BYTES" if kind == "svg" else "MAX_COMPARISON_PNG_BYTES"
    monkeypatch.setattr(evidence, constant, total - extra)
    comparison_renderer.clear()
    result = evidence.rasterization_comparisons(COMPARISON_SVG, framed, PNG)
    assert result["complete"] is (extra == 0)
    assert result[f"combined_{kind}_byte_length"] == total
    for identity, record in result["variants"].items():
        assert record[kind]["byte_length"] == baseline["variants"][identity][kind]["byte_length"]
        assert record[kind]["sha256"] == baseline["variants"][identity][kind]["sha256"]
        assert ("content" in record[kind]) is (extra == 0)
    if extra:
        assert f"combined_{kind}_byte_limit" in result["missing"]
        if kind == "svg":
            assert comparison_renderer == []


@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_renderer_failure_retains_other_variants_and_marks_incomplete(
    monkeypatch: pytest.MonkeyPatch, comparison_renderer: list[str], error_type: type[Exception]
) -> None:
    def render(svg: str) -> bytes:
        if svg == COMPARISON_SVG:
            message = "simulated renderer failure"
            raise error_type(message)
        return PNG

    monkeypatch.setattr(evidence, "_render_svg", render)
    result = evidence.rasterization_comparisons(
        COMPARISON_SVG, capture._frame_svg(COMPARISON_SVG), PNG
    )
    assert result["complete"] is False
    failed = result["variants"]["raw_original"]
    assert failed["status"] == "failed"
    assert failed["svg"]["content"] == COMPARISON_SVG
    assert failed["reason"] == error_type.__name__ + ": simulated renderer failure"
    assert result["variants"]["framed_original"]["status"] == "ok"
    assert result["variants"]["raw_dejavu"]["status"] == "ok"


@pytest.mark.parametrize("png", [b"bad", b"x" * 24, PNG[:16] + struct.pack(">II", 0, 1)])
def test_invalid_png_dimensions_cannot_complete_comparisons(
    png: bytes, comparison_renderer: list[str]
) -> None:
    result = evidence.rasterization_comparisons(
        COMPARISON_SVG, capture._frame_svg(COMPARISON_SVG), png
    )
    assert result["complete"] is False
    assert result["variants"]["framed_original"]["status"] == "failed"


def test_output_dimensions_are_observed_instead_of_assumed(
    monkeypatch: pytest.MonkeyPatch, comparison_renderer: list[str]
) -> None:
    # Renderer-boundary fake: different header dimensions in raw and framed output.
    raw_png = PNG[:16] + struct.pack(">II", 2556, 1197) + PNG[24:]
    frame_png = PNG[:16] + struct.pack(">II", 1280, 800) + PNG[24:]
    monkeypatch.setattr(evidence, "_render_svg", lambda _svg: raw_png)
    result = evidence.rasterization_comparisons(
        COMPARISON_SVG, capture._frame_svg(COMPARISON_SVG), frame_png
    )
    assert result["variants"]["raw_original"]["png"]["dimensions"] == [2556, 1197]
    assert result["variants"]["framed_original"]["png"]["dimensions"] == [1280, 800]


def test_renderer_wrapper_passes_only_exact_svg(monkeypatch: pytest.MonkeyPatch) -> None:
    def render(**arguments: Any) -> bytes:
        assert arguments == {"svg_string": COMPARISON_SVG}
        return PNG

    monkeypatch.setitem(sys.modules, "resvg_py", SimpleNamespace(svg_to_bytes=render))
    assert evidence._render_svg(COMPARISON_SVG) == PNG


@pytest.mark.parametrize("scene", ["database-picker", "find-replace", "editor"])
def test_comparisons_are_picker_only_and_published_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scene: str
) -> None:
    geometry, screenshot = tmp_path / "scene.json", tmp_path / "scene.png"
    exports: list[str] = []
    calls: list[tuple[str, str, bytes]] = []
    incomplete = {"schema_version": 1, "complete": False, "missing": ["test failure"]}

    def export(**_arguments: Any) -> str:
        exports.append(COMPARISON_SVG)
        return COMPARISON_SVG

    def compare(raw: str, framed: str, png: bytes) -> dict[str, Any]:
        assert json.loads(geometry.read_text(encoding="utf-8")) == {"ok": True}
        calls.append((raw, framed, png))
        return incomplete

    monkeypatch.setattr(capture, "_scene_ready", lambda _app: True)
    monkeypatch.setattr(
        capture,
        "_write_evidence",
        lambda *_args: geometry.write_text('{"ok": true}', encoding="utf-8"),
    )
    monkeypatch.setattr(capture, "terminal_diagnostics", lambda *_args: {})
    monkeypatch.setattr(capture, "render_diagnostics", lambda *_args: {"complete": False})
    monkeypatch.setattr(capture, "export_input_evidence", lambda *_args: {"unchanged": True})
    monkeypatch.setattr(capture, "rasterization_comparisons", compare)
    monkeypatch.setattr(capture, "_write_png", lambda _svg, path, **_kwargs: path.write_bytes(PNG))
    capture._capture_frame(
        SimpleNamespace(export_screenshot=export),
        scene,
        geometry,
        tmp_path / "manifest",
        screenshot,
    )
    saved = json.loads(geometry.read_text(encoding="utf-8"))
    assert exports == [COMPARISON_SVG]
    assert screenshot.read_bytes() == PNG
    if scene == "database-picker":
        assert calls == [(COMPARISON_SVG, capture._frame_svg(COMPARISON_SVG), PNG)]
        assert saved["rasterization_comparisons"] == incomplete
        assert saved["export_input_evidence"] == {"unchanged": True}
        assert saved["render_diagnostics"]["complete"] is False
    else:
        assert not calls
        assert "rasterization_comparisons" not in saved
    assert not geometry.with_suffix(".tmp").exists()


@pytest.mark.parametrize("ready", [False, True])
def test_picker_comparisons_cannot_bypass_readiness_or_geometry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ready: bool
) -> None:
    geometry, screenshot = tmp_path / "scene.json", tmp_path / "scene.png"
    geometry.write_text(
        '{"ok": true, "rasterization_comparisons": {"complete": true}}', encoding="utf-8"
    )

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("A rejected frame must not export or render comparisons")

    def rejected_geometry(*_args: Any) -> None:
        geometry.write_text(
            '{"ok": false, "assertions": {"required_control_visible": false}}', encoding="utf-8"
        )

    monkeypatch.setattr(capture, "_scene_ready", lambda _app: ready)
    monkeypatch.setattr(capture, "_write_evidence", rejected_geometry)
    monkeypatch.setattr(capture, "_write_png", unexpected)
    monkeypatch.setattr(capture, "rasterization_comparisons", unexpected)
    message = "failed geometry" if ready else "not ready"
    with pytest.raises(RuntimeError, match=message):
        capture._capture_frame(
            SimpleNamespace(export_screenshot=unexpected),
            "database-picker",
            geometry,
            tmp_path / "manifest",
            screenshot,
        )
    if ready:
        saved = json.loads(geometry.read_text(encoding="utf-8"))
        assert saved["ok"] is False
        assert "rasterization_comparisons" not in saved
    else:
        assert not geometry.exists()
    assert not screenshot.exists()
    assert not geometry.with_suffix(".tmp").exists()
