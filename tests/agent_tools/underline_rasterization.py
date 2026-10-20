"""Actual-resvg causal checks; mandatory in ordinary matrix and coverage jobs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import sys
from contextlib import ExitStack
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import resvg_py
from agent_tools.sql_explorer_svg_underlines import normalize_terminal_underlines
from PIL import Image

from agent_tools import sql_explorer_svg_evidence as evidence
from tests._support.paths import REPO_ROOT
from tests.agent_tools._support.raster_fonts import SYNTHETIC_FAMILY, observe_test_font
from tests.agent_tools._support.raster_observations import collect_report, write_report
from tests.agent_tools._support.svg_fixtures import decode_captured_svg

_FIXTURES = REPO_ROOT / "tests/agent_tools/_support/fixtures"
_MIN_CONTRAST = 32
_MIN_WIDTH_FRACTION = 0.7


@pytest.fixture(scope="module", autouse=True)
def retain_all_raster_cases_before_assertions() -> None:
    output = os.environ.get("RASTER_OBSERVATIONS")
    if output is not None and not Path(output).exists():
        write_report(Path(output), collect_report(sys.modules[__name__]))


def _raster(svg: str) -> Image.Image:
    intermediate = bytes(resvg_py.svg_to_bytes(svg_string=evidence.supersampling_svg(svg)))
    with Image.open(BytesIO(intermediate)) as image:
        assert image.size == (2560, 1600)
        return image.convert("RGB").resize((1280, 800), Image.Resampling.LANCZOS)


def _without_underline(svg: str) -> str:
    without_css = re.sub(r"(text-decoration\s*:\s*)underline", r"\1none", svg)
    return without_css.replace('text-decoration="underline"', 'text-decoration="none"')


def _target_regions(svg: str, cells: list[list[int]]) -> list[tuple[float, float, float]]:
    """Independent mapping of the retained Rich grid through the nested SVG viewport."""
    root = ET.fromstring(svg)
    screen = next(child for child in root if child.tag.endswith("}svg"))
    vx, vy, width, height = map(float, screen.attrib["viewBox"].split())
    scale = min(1280 / width, 800 / height)
    offset_x, offset_y = (1280 - width * scale) / 2, (800 - height * scale) / 2
    matrix = next(node for node in screen.iter() if node.get("class", "").endswith("-matrix"))
    parents = {child: parent for parent in screen.iter() for child in parent}
    translated = parents[matrix]
    transform = re.fullmatch(r"translate\(([\d.]+),\s*([\d.]+)\)", translated.attrib["transform"])
    assert transform is not None
    tx, ty = map(float, transform.groups())
    line_clips = {
        int(node.attrib["id"].rsplit("-", 1)[-1]): node[0]
        for node in screen.iter()
        if re.search(r"-line-\d+$", node.get("id", ""))
    }
    cell_width = float(line_clips[0].attrib["width"]) / 208
    regions = []
    for column, row in cells:
        clip = line_clips[row]
        candidates = [
            node
            for node in matrix.iter()
            if node.tag.endswith("}text")
            and math.isclose(float(node.get("x", "-1")), column * cell_width)
            and node.get("clip-path", "").endswith(f"-line-{row})")
        ]
        assert len(candidates) == 1
        node = candidates[0]
        baseline = float(node.attrib["y"])
        assert (
            float(clip.attrib["y"])
            < baseline
            < (float(clip.attrib["y"]) + float(clip.attrib["height"]))
        )
        x = (float(node.attrib["x"]) + tx - vx) * scale + offset_x
        y = (baseline + ty - vy) * scale + offset_y
        regions.append((x, y, float(node.attrib["textLength"]) * scale))
    return regions


def _visible_underline(
    image: Image.Image,
    control: Image.Image,
    region: tuple[float, float, float],
) -> bool:
    """Require a continuous bright underline below the baseline, not a period."""
    x, baseline, width = region
    left, right = math.ceil(x), math.floor(x + width)
    required = math.ceil(width * _MIN_WIDTH_FRACTION)
    for y in range(math.ceil(baseline), math.ceil(baseline) + 3):
        run = 0
        for column in range(left, right):
            actual, plain = image.getpixel((column, y)), control.getpixel((column, y))
            assert isinstance(actual, tuple)
            assert isinstance(plain, tuple)
            changed = all(a - b >= _MIN_CONTRAST for a, b in zip(actual, plain))
            run = run + 1 if changed else 0
            if run >= required:
                return True
    return False


@pytest.mark.parametrize("scene", ["editor-command-caret", "keyboard-multicursor"])
def test_captured_original_observation_and_normalized_causal_visibility(
    scene: str,
) -> None:
    provenance_path = _FIXTURES / "caret-export-provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    fixture = next(item for item in provenance["fixtures"] if item["scene"] == scene)
    data = (_FIXTURES / fixture["path"]).read_bytes()
    svg = decode_captured_svg(
        data,
        sha256=fixture["sha256"],
        envelope_sha256=fixture["envelope_sha256"],
    )
    normalized, conversions = normalize_terminal_underlines(svg)
    assert conversions
    regions = _target_regions(svg, fixture["screen_cells"])
    observations = {
        "captured_svg_sha256": fixture["sha256"],
        "fixture_envelope_sha256": hashlib.sha256(data).hexdigest(),
        "python": platform.python_version(),
        "resvg-py": version("resvg-py"),
        "Pillow": version("Pillow"),
        "fonts": evidence._font_context(svg),
        "qualification": "Available/system matches do not prove renderer-selected identity.",
    }
    assert evidence._comparison_font()["available"], observations
    with ExitStack() as stack:
        original = stack.enter_context(_raster(svg))
        plain = stack.enter_context(_raster(_without_underline(svg)))
        repaired = stack.enter_context(_raster(normalized))
        removed = stack.enter_context(_raster(_without_generated_strokes(normalized)))
        observations["original_visibility"] = [
            _visible_underline(original, plain, region) for region in regions
        ]
        for region in regions:
            assert not _visible_underline(plain, plain, region)
            # Recovery pixels establish that originals can already meet this
            # predicate. Record that observation without inventing a failing premise.
            assert not _visible_underline(removed, plain, region), observations
            assert _visible_underline(repaired, plain, region), observations
            wrong_cell = (region[0] + region[2] * 3, region[1], region[2])
            assert not _visible_underline(repaired, plain, wrong_cell)
    original_text = [
        (node.text, node.tail, _text_attributes(node))
        for node in ET.fromstring(svg).iter()
        if node.tag.endswith(("}text", "}tspan"))
    ]
    repaired_text = [
        (node.text, node.tail, _text_attributes(node))
        for node in ET.fromstring(normalized).iter()
        if node.tag.endswith(("}text", "}tspan"))
    ]
    assert repaired_text == original_text
    assert normalize_terminal_underlines(normalized) == (normalized, [])


def _without_generated_strokes(svg: str) -> str:
    root = ET.fromstring(svg)
    for parent in root.iter():
        for child in list(parent):
            if child.get("data-terminal-underline") == "1":
                parent.remove(child)
    return ET.tostring(root, encoding="unicode")


def _text_attributes(node: ET.Element) -> dict[str, str]:
    # Class renaming and removal of represented decorations are intentional;
    # every glyph/layout attribute and all unrelated styling must remain.
    attributes = dict(node.attrib)
    if "class" in attributes:
        attributes["class"] = attributes["class"].replace("-terminal-geometry", "")
    return attributes


def _whitespace_svg(text: str, width: float, *, clipped: bool = False) -> str:
    clip_width = 1 if clipped else width
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800">'
        '<rect width="1280" height="800" fill="#20252a"/>'
        '<defs><clipPath id="cell"><rect x="200" y="180" '
        f'width="{clip_width}" height="25"/></clipPath></defs>'
        f'<g class="terminal-probe-matrix" font-family="{SYNTHETIC_FAMILY}" font-size="20">'
        f'<text x="200" y="200" textLength="{width}" fill="#e2e2e2" '
        f'clip-path="url(#cell)" text-decoration="underline">{text}</text></g></svg>'
    )


@pytest.mark.parametrize(
    ("text", "width"),
    [(" ", 12.2), ("    ", 48.8), ("界", 24.4), ("é", 12.2)],
)
def test_real_raster_whitespace_expanded_tabs_and_unicode_with_causal_negative_controls(
    text: str,
    width: float,
) -> None:
    font = observe_test_font(text)
    assert font["complete"], font
    assert not font["missing_glyph_control_covered"], font
    svg = _whitespace_svg(text, width)
    normalized, records = normalize_terminal_underlines(svg)
    assert records[0]["width"] == width
    region = (200.0, 200.0, width)
    with _raster(_without_underline(svg)) as plain, _raster(normalized) as repaired:
        assert _visible_underline(repaired, plain, region)
        with _raster(_without_generated_strokes(normalized)) as removed:
            assert not _visible_underline(removed, plain, region)
        if not text.isspace():
            # A known absent codepoint must not be confused with the test glyph.
            missing = _whitespace_svg("\U0010ffff", width)
            with _raster(_without_underline(missing)) as tofu:
                assert tofu.crop((200, 180, math.ceil(200 + width), 200)).tobytes() != (
                    plain.crop((200, 180, math.ceil(200 + width), 200)).tobytes()
                )
        wrong_cell = (region[0] + region[2] * 3, region[1], region[2])
        assert not _visible_underline(repaired, plain, wrong_cell)
        root = ET.fromstring(normalized)
        line = next(node for node in root.iter() if node.get("data-terminal-underline") == "1")
        line.set("stroke", "#0000ff")
        with _raster(ET.tostring(root, encoding="unicode")) as wrong_color:
            assert not _visible_underline(wrong_color, plain, region)
    clipped, _ = normalize_terminal_underlines(_whitespace_svg(text, width, clipped=True))
    with _raster(clipped) as image, _raster(_without_underline(svg)) as plain:
        assert not _visible_underline(image, plain, region)


@pytest.mark.parametrize("origin", ["inline", "class", "inherited"])
def test_actual_raster_removes_native_decoration_at_each_supported_origin(origin: str) -> None:
    svg = _whitespace_svg(" ", 12.2)
    if origin == "inline":
        svg = svg.replace('text-decoration="underline"', 'style="text-decoration:underline"')
    elif origin == "class":
        css = "<style>.caret {text-decoration:underline}</style>"
        svg = svg.replace("<defs>", css + "<defs>")
        svg = svg.replace('text-decoration="underline"', 'class="caret"')
    else:
        svg = svg.replace('text-decoration="underline"', "").replace(
            'font-size="20"', 'font-size="20" text-decoration="underline"'
        )
    normalized, records = normalize_terminal_underlines(svg)
    assert len(records) == 1
    region = (200.0, 200.0, 12.2)
    with ExitStack() as stack:
        plain = stack.enter_context(_raster(_without_underline(svg)))
        repaired = stack.enter_context(_raster(normalized))
        removed = stack.enter_context(_raster(_without_generated_strokes(normalized)))
        assert _visible_underline(repaired, plain, region)
        assert not _visible_underline(removed, plain, region)
