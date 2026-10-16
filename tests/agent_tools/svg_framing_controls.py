from __future__ import annotations

import base64
import json
from xml.etree import ElementTree as ET

import pytest

from agent_tools import sql_explorer_svg_evidence as evidence
from agent_tools import sql_explorer_visual_capture as capture
from tests.agent_tools.svg_evidence import COMPARISON_SVG, PNG, comparison_renderer

# Reuse the fake font/renderer fixture; these tests never render or probe fonts.
__all__ = ["comparison_renderer"]
CONTROL_NAMES = ("raw_xml_native", "framed_preserved_target", "framed_xml_native")
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"


def _screen(svg: str) -> ET.Element:
    root = ET.fromstring(svg)
    screen = root if root.get("viewBox") else root.find(SVG_NAMESPACE + "svg")
    assert screen is not None
    return screen


def _layout(svg: str) -> str:
    root = _screen(svg)
    for name in ("width", "height", "preserveAspectRatio"):
        root.attrib.pop(name, None)
    return ET.tostring(root, encoding="unicode")


def test_controls_separate_serialization_from_scaling_and_preserve_layout(
    comparison_renderer: list[str],
) -> None:
    original = COMPARISON_SVG.replace("<g class=", "<!-- exact interior whitespace -->\n<g class=")
    framed = capture._frame_svg(original)
    ordinary_before = framed.encode("utf-8"), PNG
    result = json.loads(json.dumps(evidence.rasterization_comparisons(original, framed, PNG)))
    assert result["complete"]
    assert len(result["variants"]) == 7
    for name in CONTROL_NAMES:
        record = result["variants"][name]
        assert record["status"] == "ok"
        svg = record["svg"]["content"]
        assert _layout(svg) == _layout(original)
        assert ".connections" in svg
        assert "font-family: Fira Code, monospace;" in svg
        assert "font-family: arial;" in svg
        assert 'transform="translate(9,22)"' in svg
        assert 'clip-path="url(#clip)"' in svg
        assert record["svg"]["sha256"] == evidence.digest(svg.encode("utf-8"))
        assert record["svg"]["byte_length"] == len(svg.encode("utf-8"))
        assert base64.b64decode(record["png"]["content"], validate=True) == PNG
        assert record["png"]["sha256"] == evidence.digest(PNG)
        assert record["png"]["dimensions"] == [1, 1]
        assert record["png"]["reused_ordinary_png"] is False
        assert record["renderer_arguments"] == {"input": "svg.content", "other_arguments": {}}
    raw_xml = ET.fromstring(result["variants"]["raw_xml_native"]["svg"]["content"])
    assert raw_xml.get("viewBox") == "0 0 2556 1196.8"
    assert raw_xml.get("width") is None
    native = ET.fromstring(result["variants"]["framed_xml_native"]["svg"]["content"])
    assert (native.get("width"), native.get("height")) == ("2556", "1196.8")
    assert (native[1].get("width"), native[1].get("height")) == ("2556", "1196.8")
    preserved = result["variants"]["framed_preserved_target"]["svg"]["content"]
    # Entire original interior, including comments, combining marks and spacing, is unchanged.
    interior = original[original.index(">") + 1 : original.rindex("</svg>")]
    assert interior in preserved
    target = ET.fromstring(preserved)
    assert (target.get("width"), target.get("height")) == ("1280", "800")
    assert (target[1].get("width"), target[1].get("height")) == ("1280", "800")
    assert target[1].get("preserveAspectRatio") == "xMidYMid meet"
    assert ordinary_before == (capture._frame_svg(original).encode("utf-8"), PNG)
    assert result["variants"]["framed_original"]["svg"]["content"] == framed
    assert not result["variants"]["framed_original"]["png"]["reused_ordinary_png"]
    assert len(comparison_renderer) == 7


@pytest.mark.parametrize(
    "root",
    [
        '<svg xmlns="http://www.w3.org/2000/svg">',
        """<svg xmlns="http://www.w3.org/2000/svg" width='99' height="88" """
        """preserveAspectRatio='none' data-note="x>y">""",
        '<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg">',
    ],
)
def test_preserved_target_changes_only_root_size_and_keeps_exact_interior(root: str) -> None:
    interior = "\n<style>.x {fill: red;}</style><!-- note -->\n<text>界e\u0301 &amp; c</text>\n"
    svg = root + interior + "</svg>"
    framed = evidence._preserved_frame(svg, "1280", "800")
    assert framed.endswith(interior + "</svg></svg>")
    assert "<?xml" not in framed
    screen = ET.fromstring(framed)[1]
    assert screen.get("width") == "1280"
    assert screen.get("height") == "800"
    assert screen.get("preserveAspectRatio") == "xMidYMid meet"


@pytest.mark.parametrize("viewbox", ["", "0 0 1", "0 0 0 1", "0 0 -1 2", "0 0 nan 2", "0 0 inf 2"])
def test_unavailable_native_size_retains_originals_and_marks_control_incomplete(
    viewbox: str, comparison_renderer: list[str]
) -> None:
    svg = COMPARISON_SVG.replace("0 0 2556 1196.8", viewbox)
    result = evidence.rasterization_comparisons(svg, capture._frame_svg(svg), PNG)
    assert not result["complete"]
    assert result["variants"]["framed_xml_native"]["status"] == "omitted"
    assert "native_viewbox" in result["variants"]["framed_xml_native"]["reason"]
    for name in ("raw_original", "framed_original", "raw_xml_native", "framed_preserved_target"):
        assert result["variants"][name]["status"] == "ok"


def test_failed_control_preserves_other_inputs_and_ordinary_capture(
    monkeypatch: pytest.MonkeyPatch, comparison_renderer: list[str]
) -> None:
    framed = capture._frame_svg(COMPARISON_SVG)
    failed_input = evidence._framing_control(COMPARISON_SVG, framed, "framed_preserved_target")

    def render(svg: str) -> bytes:
        if svg == failed_input:
            message = "control failure"
            raise RuntimeError(message)
        return PNG

    monkeypatch.setattr(evidence, "_render_svg", render)
    result = evidence.rasterization_comparisons(COMPARISON_SVG, framed, PNG)
    assert not result["complete"]
    failed = result["variants"]["framed_preserved_target"]
    assert failed["status"] == "failed"
    assert failed["svg"]["content"] == failed_input
    assert "control failure" in failed["reason"]
    assert result["variants"]["framed_original"]["status"] == "ok"
    assert result["variants"]["raw_xml_native"]["status"] == "ok"
    assert result["variants"]["framed_xml_native"]["status"] == "ok"


@pytest.mark.parametrize("kind", ["svg", "png"])
@pytest.mark.parametrize("extra", [0, 1])
def test_controls_share_original_comparison_budgets(
    monkeypatch: pytest.MonkeyPatch, comparison_renderer: list[str], kind: str, extra: int
) -> None:
    framed = capture._frame_svg(COMPARISON_SVG)
    baseline = evidence.rasterization_comparisons(COMPARISON_SVG, framed, PNG)
    total = baseline[f"combined_{kind}_byte_length"]
    assert total == sum(record[kind]["byte_length"] for record in baseline["variants"].values())
    limit = "MAX_SVG_BYTES" if kind == "svg" else "MAX_COMPARISON_PNG_BYTES"
    monkeypatch.setattr(evidence, limit, total - extra)
    comparison_renderer.clear()
    result = evidence.rasterization_comparisons(COMPARISON_SVG, framed, PNG)
    assert result["complete"] is (extra == 0)
    for name in CONTROL_NAMES:
        record = result["variants"][name]
        assert ("content" in record[kind]) is (extra == 0)
        assert record[kind]["sha256"] == baseline["variants"][name][kind]["sha256"]
    if extra and kind == "svg":
        assert comparison_renderer == []
