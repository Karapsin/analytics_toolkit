"""Restore only Rich's omitted ASCII-space underline spans from the same export."""

from __future__ import annotations

import math
import re
from hashlib import sha256
from typing import Any
from xml.etree import ElementTree as ET

from agent_tools.sql_explorer_svg_phase import _transform
from agent_tools.sql_explorer_svg_underlines import _class_rules, _local_style, _number

_NS = "http://www.w3.org/2000/svg"
_FONT_SIZE = 20
_LINE_HEIGHT = 24.4
_CLIP_OFFSET = 1.5
_CLIP_HEIGHT = 24.65
_TOLERANCE = 1e-6


def _invalid(reason: str) -> ValueError:
    return ValueError(reason)


def _track(observation: dict[str, Any], raw_svg: str) -> dict[str, Any]:
    invocations = observation.get("invocations", [])
    if len(invocations) != 1:
        message = "expected_one_recording_export"
        raise _invalid(message)
    invocation = invocations[0]
    if invocation.get("raw_svg_sha256") != sha256(raw_svg.encode("utf-8")).hexdigest():
        message = "snapshot_export_identity_mismatch"
        raise _invalid(message)
    track = invocation.get("recording", {}).get("export_coordinates", {})
    if track.get("complete") is not True:
        message = "incomplete_export_coordinates"
        raise _invalid(message)
    return track


def _validate_matrix_context(root: ET.Element, matrix: ET.Element, prefix: str) -> None:
    parents = {child: parent for parent in root.iter() for child in parent}
    parent = parents.get(matrix)
    if parent is None or parent.get("clip-path") != f"url(#{prefix}-clip-terminal)":
        message = "unsupported_terminal_matrix_ancestry"
        raise _invalid(message)
    rules = _class_rules(root)
    node: ET.Element | None = matrix
    while node is not None:
        _transform(node.get("transform", ""))
        style = _local_style(node, rules)
        if (
            any(key in node.attrib for key in ("mask", "filter", "dx", "dy", "rotate"))
            or any(key in style for key in ("transform", "transform-origin", "mask", "filter"))
            or style.get("text-anchor", "start") != "start"
        ):
            message = "unsupported_whitespace_geometric_context"
            raise _invalid(message)
        node = parents.get(node)


def _matrix(root: ET.Element) -> tuple[ET.Element, str, float]:
    matrices = [
        node
        for node in root.iter()
        if any(name.endswith("-matrix") for name in node.get("class", "").split())
    ]
    if len(matrices) != 1 or matrices[0].tag != f"{{{_NS}}}g":
        message = "ambiguous_terminal_matrix"
        raise _invalid(message)
    matrix = matrices[0]
    names = [name for name in matrix.get("class", "").split() if name.endswith("-matrix")]
    style = _local_style(matrix, _class_rules(root))
    size = _number(style.get("font-size", ""), pixels=True)
    # Rich's exporter fixes the 20-unit font and 1.22 line-height. A different
    # template needs a reviewed mapping, not an inferred baseline.
    if size != _FONT_SIZE or style.get("line-height") != "24.4px":
        message = "unsupported_export_template"
        raise _invalid(message)
    prefix = names[0][: -len("-matrix")]
    _validate_matrix_context(root, matrix, prefix)
    return matrix, prefix, size


def _line_clip(
    root: ET.Element, prefix: str, row: int, width: float, console_width: int
) -> str:
    identity = f"{prefix}-line-{row}"
    clips = [node for node in root.iter() if node.get("id") == identity]
    if len(clips) != 1 or clips[0].tag != f"{{{_NS}}}clipPath" or len(clips[0]) != 1:
        message = "ambiguous_or_absent_line_clip"
        raise _invalid(message)
    clip = clips[0]
    rectangle = clip[0]
    expected = {
        "x": 0.0,
        "y": row * _LINE_HEIGHT + _CLIP_OFFSET,
        "width": width * console_width,
        "height": _CLIP_HEIGHT,
    }
    if (
        clip.get("clipPathUnits", "userSpaceOnUse") != "userSpaceOnUse"
        or rectangle.tag != f"{{{_NS}}}rect"
        or set(rectangle.attrib) != set(expected)
        or any(
            not math.isclose(_number(rectangle.attrib[key]), value)
            for key, value in expected.items()
        )
    ):
        message = "unsupported_export_line_clip"
        raise _invalid(message)
    return identity


def _space_record(segment: dict[str, Any]) -> bool:
    text = segment["text"]
    return bool(text) and text == " " * len(text) and bool(segment["style"]["flags"]["underline"])


def _restore_span(context: dict[str, Any], segment: dict[str, Any]) -> dict[str, Any] | None:
    root, matrix, prefix, size, width, console_width = (
        context[key] for key in ("root", "matrix", "prefix", "size", "width", "console_width")
    )
    x, row, cells = (segment[key] for key in ("x", "y", "cells"))
    if (
        any(type(value) is not int or value < 0 for value in (x, row, cells))
        or cells != len(segment["text"])
        or not cells
        or x + cells > console_width
        or segment["style"]["flags"]["strike"]
        or segment["style"]["flags"]["conceal"]
    ):
        message = "unsupported_whitespace_span"
        raise _invalid(message)
    clip_id = _line_clip(root, prefix, row, width, console_width)
    left, right = x * width, (x + cells) * width
    baseline = row * _LINE_HEIGHT + size
    color = segment["resolved"]["foreground"]
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        message = "unresolved_whitespace_color"
        raise _invalid(message)
    identity = f"{row}:{x}:{cells}:{color}"
    existing = [
        node
        for node in matrix.iter()
        if node.get("data-terminal-whitespace") == identity
    ]
    # The supported Rich template places each text directly in the matrix.
    # Any overlapping text makes omission unproven; do not draw over it.
    for node in matrix:
        if node.tag != f"{{{_NS}}}text":
            continue
        if not math.isclose(_number(node.get("y", "")), baseline):
            continue
        start = _number(node.get("x", ""))
        end = start + _number(node.get("textLength", ""))
        if start < right - _TOLERANCE and end > left + _TOLERANCE:
            message = "whitespace_omission_not_proven"
            raise _invalid(message)
        if node.get("clip-path") != f"url(#{clip_id})":
            message = "inconsistent_export_baseline_or_clip"
            raise _invalid(message)
    y = baseline + size * 0.1
    attributes = {
        "x1": str(left),
        "x2": str(right),
        "y1": str(y),
        "y2": str(y),
        "stroke": color,
        "stroke-width": str(size * 0.1),
        "stroke-linecap": "butt",
        "fill": "none",
        "clip-path": f"url(#{clip_id})",
        "data-terminal-underline": "1",
        "data-terminal-whitespace": identity,
    }
    if existing:
        if len(existing) != 1 or existing[0].attrib != attributes:
            message = "inconsistent_existing_whitespace_decoration"
            raise _invalid(message)
        return None
    ET.SubElement(matrix, f"{{{_NS}}}line", attributes)
    return {
        "export_cell": [x, row],
        "cells": cells,
        "line_clip": clip_id,
        "baseline": baseline,
        "color": color,
        "attributes": attributes,
        "source": "same-invocation filtered/cropped ASCII-space-only underline",
    }


def restore_omitted_underlines(
    svg: str, observation: dict[str, Any], *, raw_svg: str | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Return an export-only copy; incomplete or ambiguous evidence raises."""
    track = _track(observation, raw_svg if raw_svg is not None else svg)
    segments = [segment for segment in track["segments"] if _space_record(segment)]
    if not segments:
        return svg, []
    root = ET.fromstring(svg)  # noqa: S314 - same-invocation generated export.
    matrix, prefix, size = _matrix(root)
    ratio = float(track["font_aspect_ratio"])
    width = size * ratio
    console_width = track["console_width"]
    if not math.isfinite(width) or width <= 0 or type(console_width) is not int or console_width <= 0:
        message = "invalid_export_cell_width"
        raise _invalid(message)
    records = []
    context = {
        "root": root,
        "matrix": matrix,
        "prefix": prefix,
        "size": size,
        "width": width,
        "console_width": console_width,
    }
    for segment in segments:
        record = _restore_span(context, segment)
        if record is not None:
            records.append(record)
    return (ET.tostring(root, encoding="unicode"), records) if records else (svg, [])
