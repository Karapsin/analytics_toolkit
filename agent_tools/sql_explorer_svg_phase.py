"""Align generated export underline centers in final pixel coordinates."""

from __future__ import annotations

import math
import re
from typing import Any
from xml.etree import ElementTree as ET

# (scale_x, scale_y, translate_x, translate_y); rotations/skews are never approximated.
IDENTITY = (1.0, 1.0, 0.0, 0.0)
_MATRIX_COMPONENTS = 6
_VIEWBOX_COMPONENTS = 4
_COORDINATE_PAIR = 2
_MAX_PHASE_DISPLACEMENT = 0.5


def _invalid(reason: str) -> ValueError:
    return ValueError(reason)


def _compose(parent: tuple[float, ...], child: tuple[float, ...]) -> tuple[float, ...]:
    a, d, e, f = parent
    x, y, tx, ty = child
    result = (a * x, d * y, a * tx + e, d * ty + f)
    if any(not math.isfinite(value) for value in result):
        message = "nonfinite_mapping"
        raise _invalid(message)
    return result


def _numbers(value: str) -> list[float]:
    values = [float(item) for item in value.replace(",", " ").split()]
    if any(not math.isfinite(item) for item in values):
        message = "nonfinite_mapping"
        raise _invalid(message)
    return values


def _transform(value: str) -> tuple[float, ...]:
    matrix: tuple[float, ...] = IDENTITY
    remainder = value
    while remainder.strip():
        match = re.match(r"\s*(translate|scale|matrix)\s*\(([^()]*)\)", remainder)
        if match is None:
            message = "unsupported_axis_transform"
            raise _invalid(message)
        name, arguments = match.groups()
        numbers = _numbers(arguments)
        if name == "translate" and len(numbers) in {1, 2}:
            ty = numbers[1] if len(numbers) == _COORDINATE_PAIR else 0.0
            current = (1.0, 1.0, numbers[0], ty)
        elif name == "scale" and len(numbers) in {1, 2}:
            current = (numbers[0], numbers[-1], 0.0, 0.0)
        elif name == "matrix" and len(numbers) == _MATRIX_COMPONENTS:
            a, b, c, d, e, f = numbers
            if b or c:
                message = "rotated_or_skewed_mapping"
                raise _invalid(message)
            current = (a, d, e, f)
        else:
            message = "unsupported_axis_transform"
            raise _invalid(message)
        matrix = _compose(matrix, current)
        remainder = remainder[match.end() :]
    if not matrix[0] or not matrix[1]:
        message = "singular_mapping"
        raise _invalid(message)
    return matrix


def _length(value: str, reference: float) -> float:
    number = float(value[:-1]) * reference / 100 if value.endswith("%") else float(value)
    if not math.isfinite(number):
        message = "nonfinite_viewport"
        raise _invalid(message)
    return number


def _aspect_mapping(
    node: ET.Element,
    scales: tuple[float, float, float, float],
    viewbox: tuple[float, float, float, float],
    dimensions: tuple[float, float],
) -> tuple[float, ...]:
    sx, sy, x, y = scales
    vx, vy, vw, vh = viewbox
    width, height = dimensions
    aspect = node.get("preserveAspectRatio", "xMidYMid meet").split()
    if aspect == ["none"]:
        mapping = (sx, sy, x - vx * sx, y - vy * sy)
    else:
        if len(aspect) == 1:
            aspect.append("meet")
        if len(aspect) != _COORDINATE_PAIR or aspect[1] not in {"meet", "slice"}:
            message = "unsupported_aspect_ratio"
            raise _invalid(message)
        alignment = re.fullmatch(r"x(Min|Mid|Max)Y(Min|Mid|Max)", aspect[0])
        if alignment is None:
            message = "unsupported_aspect_ratio"
            raise _invalid(message)
        scale = min(sx, sy) if aspect[1] == "meet" else max(sx, sy)
        factors = {"Min": 0.0, "Mid": 0.5, "Max": 1.0}
        dx = (width - vw * scale) * factors[alignment[1]]
        dy = (height - vh * scale) * factors[alignment[2]]
        mapping = (scale, scale, x + dx - vx * scale, y + dy - vy * scale)
    return mapping


def _viewport(
    node: ET.Element, parent_size: tuple[float, float]
) -> tuple[tuple[float, ...], tuple[float, float]]:
    width = _length(node.get("width", "100%"), parent_size[0])
    height = _length(node.get("height", "100%"), parent_size[1])
    x = _length(node.get("x", "0"), parent_size[0])
    y = _length(node.get("y", "0"), parent_size[1])
    if width <= 0 or height <= 0:
        message = "empty_viewport"
        raise _invalid(message)
    values = _numbers(node.get("viewBox", f"0 0 {width} {height}"))
    if len(values) != _VIEWBOX_COMPONENTS:
        message = "invalid_viewbox"
        raise _invalid(message)
    vx, vy, vw, vh = values
    if vw <= 0 or vh <= 0:
        message = "empty_viewbox"
        raise _invalid(message)
    sx, sy = width / vw, height / vh
    mapping = _aspect_mapping(node, (sx, sy, x, y), (vx, vy, vw, vh), (width, height))
    return mapping, (vw, vh)


def _aligned_stroke(node: ET.Element, matrix: tuple[float, ...]) -> dict[str, Any]:
    y1, y2 = float(node.attrib["y1"]), float(node.attrib["y2"])
    if not math.isfinite(y1) or y1 != y2:
        message = "nonhorizontal_generated_stroke"
        raise _invalid(message)
    center = matrix[1] * y1 + matrix[3]
    if not math.isfinite(center):
        message = "nonfinite_stroke_center"
        raise _invalid(message)
    target = math.floor(center) + 0.5
    displacement = target - center
    local = y1 + displacement / matrix[1]
    if not math.isfinite(local) or abs(displacement) > _MAX_PHASE_DISPLACEMENT:
        message = "invalid_phase_displacement"
        raise _invalid(message)
    node.set("y1", repr(local))
    node.set("y2", repr(local))
    return {
        "local_y_before": y1,
        "local_y_after": local,
        "final_center_before": center,
        "final_center_after": target,
        "final_displacement": displacement,
        "axis_mapping": list(matrix),
        "stroke_attributes_after": dict(node.attrib),
    }


def align_generated_underlines(
    svg: str, viewport: tuple[int, int] = (1280, 800)
) -> tuple[str, list[dict[str, Any]]]:
    """Return an export copy; unsupported geometry raises, never approximates."""
    if any(size <= 0 for size in viewport):
        message = "empty_target_viewport"
        raise _invalid(message)
    root = ET.fromstring(svg)  # noqa: S314 - generated normalized terminal SVG.
    parents = {child: parent for parent in root.iter() for child in parent}
    strokes = [node for node in root.iter() if node.get("data-terminal-underline") == "1"]
    if not strokes:
        return svg, []
    # A CSS transform or CSS geometry could override the attribute mapping.
    for node in root.iter():
        style = (node.text or "") if node.tag.rsplit("}", 1)[-1] == "style" else ""
        if re.search(
            r"(?:^|[;{])\s*(?:transform|transform-origin|x|y|width|height)\s*:",
            style + ";" + node.get("style", ""),
        ):
            message = "css_mapping_unavailable"
            raise _invalid(message)
    root_width = _length(root.get("width", str(viewport[0])), viewport[0])
    root_height = _length(root.get("height", str(viewport[1])), viewport[1])
    if root_width <= 0 or root_height <= 0:
        message = "empty_root_viewport"
        raise _invalid(message)
    records = []
    for stroke in strokes:
        chain = [stroke]
        while chain[-1] in parents:
            chain.append(parents[chain[-1]])
        matrix: tuple[float, ...] = (viewport[0] / root_width, viewport[1] / root_height, 0.0, 0.0)
        size = (root_width, root_height)
        for node in reversed(chain):
            tag = node.tag.rsplit("}", 1)[-1]
            if tag not in {"svg", "g", "line"}:
                message = "unsupported_stroke_ancestor"
                raise _invalid(message)
            matrix = _compose(matrix, _transform(node.get("transform", "")))
            if tag == "svg":
                mapping, size = _viewport(node, size)
                matrix = _compose(matrix, mapping)
        records.append(_aligned_stroke(stroke, matrix))
    return ET.tostring(root, encoding="unicode"), records
