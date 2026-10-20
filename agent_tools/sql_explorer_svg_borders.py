"""Diagnostic-only continuity strokes for verified decorative vertical border cells."""

from __future__ import annotations

import math
from io import BytesIO
from typing import Any
from xml.etree import ElementTree as ET

from rich.cells import cell_len

from agent_tools.sql_explorer_svg_phase import _compose, _transform, _viewport
from agent_tools.sql_explorer_svg_underlines import _class_rules, _local_style, _number
from agent_tools.sql_explorer_svg_whitespace import _line_clip, _matrix, _track

_NS = "http://www.w3.org/2000/svg"
_MIN_RUN_CELLS = 2
_TRIAL_THICKNESS_FACTOR = 0.05
_MAX_PIXEL_SAMPLES = 4096


def border_pixel_observations(svg: str, png: bytes, control_png: bytes) -> dict[str, Any]:
    """Retain actual stage pixels at mapped trial centers; supplies no adoption verdict."""
    from PIL import Image  # noqa: PLC0415 - GitHub-only raster dependency.

    root = ET.fromstring(svg)  # noqa: S314 - generated diagnostic SVG.
    parents = {child: parent for parent in root.iter() for child in parent}
    samples = []
    with Image.open(BytesIO(png)) as source, Image.open(BytesIO(control_png)) as reference:
        with source.convert("RGB") as image, reference.convert("RGB") as control:
            if image.size != control.size:
                return {"complete": False, "missing": ["stage_dimensions_differ"]}
            width, height = image.size
            rw, rh = float(root.get("width", width)), float(root.get("height", height))
            for line in root.iter():
                if line.get("data-terminal-border-trial") != "1":
                    continue
                chain = [line]
                while chain[-1] in parents:
                    chain.append(parents[chain[-1]])
                matrix = (width / rw, height / rh, 0.0, 0.0)
                size = (rw, rh)
                for node in reversed(chain):
                    matrix = _compose(matrix, _transform(node.get("transform", "")))
                    if node.tag.rsplit("}", 1)[-1] == "svg":
                        viewport, size = _viewport(node, size)
                        matrix = _compose(matrix, viewport)
                a, d, e, f = matrix
                x = float(line.attrib["x1"]) * a + e
                ends = sorted(float(line.attrib[key]) * d + f for key in ("y1", "y2"))
                column = math.floor(x)
                for y in range(math.ceil(ends[0]), math.floor(ends[1]) + 1):
                    if not 0 <= column < width or not 0 <= y < height:
                        continue
                    if len(samples) >= _MAX_PIXEL_SAMPLES:
                        return {
                            "complete": False,
                            "missing": ["border_pixel_sample_limit"],
                            "samples": samples,
                        }
                    actual, original = image.getpixel((column, y)), control.getpixel((column, y))
                    samples.append(
                        {
                            "pixel": [column, y],
                            "rgb_trial": actual,
                            "rgb_control": original,
                            "axis_mapping": list(matrix),
                        }
                    )
    return {
        "complete": bool(samples),
        "missing": [] if samples else ["no_trial_samples"],
        "samples": samples,
        "qualification": "observations only; independent review required",
    }


def _export_cell(track: dict[str, Any], x: int, y: int) -> dict[str, Any] | None:
    matches = [
        segment
        for segment in track["segments"]
        if segment["y"] == y and segment["x"] <= x < segment["x"] + segment["cells"]
    ]
    if len(matches) != 1:
        return None
    segment = matches[0]
    text = segment["text"]
    if any(cell_len(character) != 1 for character in text):
        return None
    if text[x - segment["x"]] != "│":
        return None
    return segment


def _verified_cells(terminal: dict[str, Any], track: dict[str, Any]) -> list[dict[str, Any]]:
    cells = []
    for entry in terminal.get("regions", []):
        region = entry["region"]
        roles = entry.get("decorative_border_roles", {})
        for sample in entry.get("border_samples", []):
            clip = sample["screen_clip"]
            row = sample.get("compositor_row")
            if (
                sample.get("layers_equal") is not True
                or sample["missing"]
                or clip["width"] != 1
                or row is None
                or row["text"] != "│"
                or not region["y"] < clip["y"] < region["y"] + region["height"] - 1
            ):
                continue
            side = None
            if clip["x"] == region["x"]:
                side = "left"
            elif clip["x"] == region["x"] + region["width"] - 1:
                side = "right"
            if side is None or roles.get(side) not in {"solid", "round"}:
                continue
            segment = _export_cell(track, clip["x"], clip["y"])
            if segment is not None:
                cells.append(
                    {
                        "x": clip["x"],
                        "y": clip["y"],
                        "segment": segment,
                        "role": entry["name"] + ":" + side,
                    }
                )
    return cells


def _svg_cell(
    matrix: ET.Element,
    cell: dict[str, Any],
    geometry: tuple[float, float, str],
    rules: list[tuple[str, dict[str, str]]],
) -> bool:
    width, size, clip_id = geometry
    x, y = cell["x"], cell["y"]
    matches = []
    for node in matrix:
        if node.tag != f"{{{_NS}}}text" or node.get("clip-path") != f"url(#{clip_id})":
            continue
        text = node.text or ""
        if not text or any(cell_len(character) != 1 for character in text):
            continue
        left = _number(node.get("x", ""))
        offset = (x * width - left) / width
        if not math.isclose(offset, round(offset)) or not 0 <= round(offset) < len(text):
            continue
        if (
            text[round(offset)] == "│"
            and math.isclose(_number(node.get("y", "")), y * 24.4 + size)
            and math.isclose(_number(node.get("textLength", "")), len(text) * width)
            and _local_style(node, rules).get("fill") == cell["segment"]["resolved"]["foreground"]
            and not any(key in node.attrib for key in ("transform", "dx", "dy", "rotate"))
        ):
            matches.append(node)
    return len(matches) == 1


def border_continuity_trial(
    svg: str, observation: dict[str, Any], terminal: dict[str, Any], *, raw_svg: str
) -> tuple[str, list[dict[str, Any]]]:
    """Keep ordinary glyphs; add only sampled, contiguous, role-verified cell strokes."""
    track = _track(observation, raw_svg)
    root = ET.fromstring(svg)  # noqa: S314 - same-invocation generated SVG.
    matrix, prefix, size = _matrix(root)
    width = size * float(track["font_aspect_ratio"])
    candidates = _verified_cells(terminal, track)
    records = []
    rules = _class_rules(root)
    for cell in candidates:
        neighbors = [
            other
            for other in candidates
            if other["role"] == cell["role"]
            and other["x"] == cell["x"]
            and abs(other["y"] - cell["y"]) <= 1
            and other["segment"]["resolved"] == cell["segment"]["resolved"]
            and other["segment"]["style"] == cell["segment"]["style"]
        ]
        if len(neighbors) < _MIN_RUN_CELLS:
            continue
        clip_id = _line_clip(root, prefix, cell["y"], width, track["console_width"])
        if not _svg_cell(matrix, cell, (width, size, clip_id), rules):
            continue
        center = (cell["x"] + 0.5) * width
        top = cell["y"] * 24.4 + 1.5
        attributes = {
            "x1": str(center),
            "x2": str(center),
            "y1": str(top),
            "y2": str(top + 24.4),
            "stroke": cell["segment"]["resolved"]["foreground"],
            "stroke-width": str(size * _TRIAL_THICKNESS_FACTOR),
            "stroke-linecap": "butt",
            "fill": "none",
            "clip-path": f"url(#{clip_id})",
            "data-terminal-border-trial": "1",
        }
        ET.SubElement(matrix, f"{{{_NS}}}line", attributes)
        records.append(
            {
                "cell": [cell["x"], cell["y"]],
                "role": cell["role"],
                "attributes": attributes,
                "font_size": size,
                "thickness_factor": _TRIAL_THICKNESS_FACTOR,
            }
        )
    return (ET.tostring(root, encoding="unicode"), records) if records else (svg, [])
