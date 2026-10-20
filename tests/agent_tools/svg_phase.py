from __future__ import annotations

import math
from copy import deepcopy
from xml.etree import ElementTree as ET

import pytest
from agent_tools.sql_explorer_svg_phase import align_generated_underlines

from tests.agent_tools.underline_rasterization import _raster, _visible_underline

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800">'
    '<rect width="1280" height="800" fill="#20252a"/>'
    '<svg width="1280" height="800" viewBox="0 0 2560 1600">'
    '<defs><clipPath id="cell"><rect x="400" y="380" width="24.4" height="30"/>'
    '</clipPath></defs><g transform="translate(0,0.6)" clip-path="url(#cell)">'
    '<line data-terminal-underline="1" x1="400" x2="424.4" y1="400" y2="400" '
    'stroke="#e2e2e2" stroke-width="2" stroke-linecap="butt"/>'
    "</g></svg></svg>"
)


def _stroke(svg: str) -> ET.Element:
    return next(
        node for node in ET.fromstring(svg).iter() if node.get("data-terminal-underline") == "1"
    )


def test_nested_viewport_maps_to_final_pixel_center_and_changes_only_vertical_center() -> None:
    original = ET.fromstring(SVG)
    aligned, records = align_generated_underlines(SVG)
    assert len(records) == 1
    record = records[0]
    assert record["axis_mapping"] == [0.5, 0.5, 0.0, 0.3]
    assert record["final_center_before"] == pytest.approx(200.3)
    assert record["final_center_after"] == 200.5
    assert record["final_displacement"] == pytest.approx(0.2)
    assert abs(record["final_displacement"]) <= 0.5
    assert record["local_y_after"] == pytest.approx(400.4)
    repaired = ET.fromstring(aligned)
    assert len(list(repaired.iter())) == len(list(original.iter()))
    for before, after in zip(original.iter(), repaired.iter()):
        assert (before.tag, before.text, before.tail) == (after.tag, after.text, after.tail)
        expected = dict(before.attrib)
        if before.get("data-terminal-underline") == "1":
            expected["y1"] = expected["y2"] = repr(record["local_y_after"])
        assert dict(after.attrib) == expected
    assert ET.tostring(original, encoding="unicode") == ET.tostring(
        ET.fromstring(SVG), encoding="unicode"
    )


@pytest.mark.parametrize(
    ("replacement", "expected_mapping"),
    [
        ('transform="translate(10,20) scale(2,3)"', [1.0, 1.5, 5.0, 10.0]),
        ('transform="matrix(2,0,0,-3,10,20)"', [1.0, -1.5, 5.0, 10.0]),
    ],
)
def test_transform_order_and_inverse_mapping(
    replacement: str, expected_mapping: list[float]
) -> None:
    svg = SVG.replace('transform="translate(0,0.6)"', replacement)
    aligned, records = align_generated_underlines(svg)
    record = records[0]
    assert record["axis_mapping"] == expected_mapping
    a, d, e, f = expected_mapping
    assert a != 0
    assert e == 5
    projected = float(_stroke(aligned).attrib["y1"]) * d + f
    assert projected == pytest.approx(record["final_center_after"])
    assert projected % 1 == pytest.approx(0.5)
    assert abs(record["final_displacement"]) <= 0.5


@pytest.mark.parametrize(
    ("aspect", "expected"),
    [
        ("xMinYMin meet", [0.5, 0.5, 0.0, 0.3]),
        ("xMaxYMax meet", [0.5, 0.5, 0.0, 200.3]),
        ("none", [0.5, 2 / 3, 0.0, 0.4]),
    ],
)
def test_viewbox_alignment_and_nonuniform_scaling(aspect: str, expected: list[float]) -> None:
    svg = SVG.replace(
        'viewBox="0 0 2560 1600"',
        f'viewBox="0 0 2560 1200" preserveAspectRatio="{aspect}"',
    )
    _, records = align_generated_underlines(svg)
    assert records[0]["axis_mapping"] == pytest.approx(expected)


@pytest.mark.parametrize(
    ("transform", "reason"),
    [
        ("rotate(15)", "unsupported_axis_transform"),
        ("skewX(2)", "unsupported_axis_transform"),
        ("matrix(1,1,0,1,0,0)", "rotated_or_skewed_mapping"),
        ("scale(0)", "singular_mapping"),
        ("translate(nan,0)", "nonfinite_mapping"),
    ],
)
def test_unsupported_or_singular_transform_is_not_approximated(
    transform: str, reason: str
) -> None:
    svg = SVG.replace("translate(0,0.6)", transform)
    with pytest.raises(ValueError, match=reason):
        align_generated_underlines(svg)


def test_css_mapping_and_definition_only_strokes_remain_unresolved() -> None:
    css = SVG.replace("<defs>", "<style>g {transform:scale(2)}</style><defs>")
    with pytest.raises(ValueError, match="css_mapping_unavailable"):
        align_generated_underlines(css)
    root = ET.fromstring(SVG)
    line = _stroke(SVG)
    root[1][0].append(line)
    with pytest.raises(ValueError, match="unsupported_stroke_ancestor"):
        align_generated_underlines(ET.tostring(root, encoding="unicode"))
    without_marker = SVG.replace('data-terminal-underline="1"', "")
    assert align_generated_underlines(without_marker) == (without_marker, [])


@pytest.mark.parametrize("offset", [0.0, 0.2, 0.6, 1.0, 1.8])
def test_real_fractional_phase_visibility_with_causal_negatives(offset: float) -> None:
    svg = SVG.replace("translate(0,0.6)", f"translate(0,{offset})")
    aligned, records = align_generated_underlines(svg)
    assert abs(records[0]["final_displacement"]) <= 0.5
    assert _stroke(aligned).attrib["stroke-width"] == "2"
    region = (200.0, 199.0, 12.2)
    root = ET.fromstring(aligned)
    parent = root[1][1]
    line = parent[0]
    removed = deepcopy(root)
    removed[1][1].remove(removed[1][1][0])
    plain_svg = ET.tostring(removed, encoding="unicode")
    with _raster(plain_svg) as plain, _raster(svg) as control, _raster(aligned) as image:
        observed_original = _visible_underline(control, plain, region)
        assert isinstance(observed_original, bool)
        assert _visible_underline(image, plain, region)
        assert not _visible_underline(plain, plain, region)
        assert not _visible_underline(image, plain, (240.0, 199.0, 12.2))
        line.set("stroke", "#0000ff")
        with _raster(ET.tostring(root, encoding="unicode")) as wrong_color:
            assert not _visible_underline(wrong_color, plain, region)
        line.set("stroke", "#e2e2e2")
        clip = root[1][0][0][0]
        clip.set("width", "1")
        with _raster(ET.tostring(root, encoding="unicode")) as clipped:
            assert not _visible_underline(clipped, plain, region)
    assert math.isclose(float(line.attrib["x2"]) - float(line.attrib["x1"]), 24.4)
