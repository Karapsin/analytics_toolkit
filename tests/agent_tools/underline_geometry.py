from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from agent_tools.sql_explorer_svg_underlines import normalize_terminal_underlines

_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg"><style>'
    '.terminal-probe-matrix {font-size:20px;fill:#abcdef}'
    '.caret {text-decoration:underline line-through}'
    '</style><defs><clipPath id="row"><rect width="100" height="40"/>'
    '</clipPath></defs><g transform="translate(9,41)" class="terminal-probe-matrix">'
    '<text class="caret" x="12.2" y="20" textLength="24.4" clip-path="url(#row)">界</text>'
    '</g></svg>'
)


def test_geometry_preserves_text_attributes_context_color_and_other_decoration() -> None:
    normalized, records = normalize_terminal_underlines(_SVG)
    assert len(records) == 1
    assert records[0]["x"] == 12.2
    assert records[0]["width"] == 24.4
    assert records[0]["baseline"] == 20
    assert records[0]["y"] == 22
    assert records[0]["thickness"] == 2
    assert records[0]["color"] == "#abcdef"
    original = ET.fromstring(_SVG)
    repaired = ET.fromstring(normalized)
    text = next(node for node in repaired.iter() if node.tag.endswith("}text"))
    old_text = next(node for node in original.iter() if node.tag.endswith("}text"))
    assert text.text == old_text.text
    assert {key: value for key, value in text.attrib.items() if key != "style"} == old_text.attrib
    assert text.attrib["style"] == ";text-decoration:line-through;text-decoration-line:line-through"
    line = next(node for node in repaired.iter() if node.tag.endswith("}line"))
    assert line.attrib["stroke-linecap"] == "butt"
    assert line.attrib["stroke"] == "#abcdef"
    parents = {child: parent for parent in repaired.iter() for child in parent}
    assert parents[line].attrib == {"clip-path": "url(#row)"}
    assert parents[parents[line]].attrib["transform"] == "translate(9,41)"
    assert normalize_terminal_underlines(normalized) == (normalized, [])


@pytest.mark.parametrize("glyph", [" ", "    ", "界", "é", "ȩ́"])
def test_actual_span_width_does_not_depend_on_codepoint_count(glyph: str) -> None:
    svg = _SVG.replace(">界<", f">{glyph}<")
    normalized, records = normalize_terminal_underlines(svg)
    assert records[0]["width"] == 24.4
    text = next(node for node in ET.fromstring(normalized).iter() if node.tag.endswith("}text"))
    assert text.text == glyph


def test_inherited_decoration_and_independently_positioned_nested_spans() -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<g class="terminal-probe-matrix" font-size="20" fill="#abcdef" '
        'text-decoration="underline" transform="scale(2)" clip-path="url(#outer)">'
        '<text transform="translate(3,4)"><tspan x="0" y="20" textLength="12.2">é</tspan>'
        '<tspan x="12.2" y="20" textLength="24.4" fill="#123456" '
        'text-decoration-color="#fedcba" clip-path="url(#inner)">界</tspan></text>'
        '</g></svg>'
    )
    normalized, records = normalize_terminal_underlines(svg)
    assert [record["width"] for record in records] == [12.2, 24.4]
    assert [record["color"] for record in records] == ["#abcdef", "#fedcba"]
    assert records[1]["contexts"] == [
        {"transform": "translate(3,4)"},
        {"clip-path": "url(#inner)"},
    ]
    assert normalize_terminal_underlines(normalized) == (normalized, [])


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ('x="12.2"', 'x="1 2"', "unsupported_underline_length"),
        ('textLength="24.4"', "", "missing_underline_geometry"),
        ('y="20"', 'y="20" dx="1"', "unsupported_relative"),
        ('textLength="24.4"', 'textLength="-1"', "ambiguous_underline"),
        ("font-size:20px", "font-size:1em", "unsupported_underline_length"),
        ("underline line-through", "underline wavy", "unsupported_underline_decoration"),
        (".caret {", "text.caret {", "unsupported_underlined_css_selector"),
        (">界</text>", ">界<tspan>e</tspan></text>", "ambiguous_mixed"),
        (">界</text>", "></text>", "empty_underlined"),
        ('y="20"', 'y="20" direction="rtl"', "unsupported_underline_text_direction"),
    ],
)
def test_ambiguous_underlines_fail_explicitly(old: str, new: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        normalize_terminal_underlines(_SVG.replace(old, new))


def test_no_underlined_terminal_span_returns_exact_input_bytes() -> None:
    svg = _SVG.replace("underline line-through", "line-through")
    assert normalize_terminal_underlines(svg) == (svg, [])
    outside_terminal = _SVG.replace('class="terminal-probe-matrix"', 'class="ordinary-title"')
    assert normalize_terminal_underlines(outside_terminal) == (outside_terminal, [])


def test_resolved_css_context_and_color_are_retained_by_the_geometry() -> None:
    svg = _SVG.replace(
        "text-decoration:underline line-through",
        "text-decoration:underline;clip-path:url(#row);opacity:0.5;"
        "text-decoration-color:currentColor;color:#123456;fill-opacity:0.75",
    )
    normalized, records = normalize_terminal_underlines(svg)
    assert records[0]["color"] == "#123456"
    root = ET.fromstring(normalized)
    line = next(node for node in root.iter() if node.tag.endswith("}line"))
    assert line.attrib["stroke-opacity"] == "0.75"
    parents = {child: parent for parent in root.iter() for child in parent}
    assert parents[line].attrib["clip-path"] == "url(#row)"
    assert parents[line].attrib["opacity"] == "0.5"
