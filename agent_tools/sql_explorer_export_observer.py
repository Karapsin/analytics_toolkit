"""Capture-scoped observations of the actual Rich SVG export, without replay."""

from __future__ import annotations

import inspect
import json
import math
import re
from contextlib import contextmanager
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from unittest.mock import patch
from xml.etree import ElementTree as ET

from rich.cells import cell_len
from rich.color import blend_rgb
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from rich.terminal_theme import SVG_EXPORT_THEME
from textual._compositor import Compositor
from textual.app import App

MAX_OBSERVATION_BYTES = 512 * 1024
MAX_SOURCE_BYTES = 16 * 1024
MAX_MODULE_BYTES = 2 * 1024 * 1024
MAX_SEGMENTS = 4096
_SVG_DIM_BLEND = 0.4
_FLAGS = ("bold", "dim", "italic", "underline", "reverse", "strike", "conceal", "blink")


def _source(function: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "function": getattr(function, "__qualname__", type(function).__qualname__),
    }
    try:
        lines, start = inspect.getsourcelines(function)
        data = "".join(lines).encode("utf-8")
        path = inspect.getsourcefile(function)
        result.update(start_line=start, byte_length=len(data), sha256=sha256(data).hexdigest())
        if len(data) <= MAX_SOURCE_BYTES:
            result["content"] = data.decode("utf-8")
        else:
            result["missing"] = "source_excerpt_byte_limit"
        if path is None:
            result["module_missing"] = "source_file_unavailable"
        else:
            with Path(path).open("rb") as stream:
                module = stream.read(MAX_MODULE_BYTES + 1)
            result["module"] = {"path": path, "byte_length": len(module)}
            if len(module) <= MAX_MODULE_BYTES:
                result["module"]["sha256"] = sha256(module).hexdigest()
            else:
                result["module_missing"] = "module_byte_limit"
    except (OSError, TypeError) as error:
        result["missing"] = type(error).__name__
    return result


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unavailable"


def dependency_sources(exporter: Any) -> dict[str, Any]:
    return {
        "versions": {
            package: _package_version(package)
            for package in ("rich", "textual", "resvg-py", "Pillow")
        },
        "sources": [
            _source(function)
            for function in (
                getattr(App, "export_screenshot", None),
                getattr(Compositor, "render_strips", None),
                getattr(Compositor, "render_update", None),
                exporter,
                Segment.filter_control,
                Segment.split_and_crop_lines,
            )
        ],
    }


def _style(style: Any) -> dict[str, Any]:
    return {
        "raw": str(style) if style is not None else None,
        "foreground": repr(style.color) if style is not None else None,
        "background": repr(style.bgcolor) if style is not None else None,
        "flags": {name: getattr(style, name) if style is not None else None for name in _FLAGS},
        "link": style.link if style is not None else None,
        "resolution": "original recorded style; not exporter-resolved color",
    }


def _resolved_colors(style: Style, theme: Any) -> dict[str, str]:
    foreground = (
        theme.foreground_color
        if style.color is None or style.color.is_default
        else style.color.get_truecolor(theme)
    )
    background = (
        theme.background_color
        if style.bgcolor is None or style.bgcolor.is_default
        else style.bgcolor.get_truecolor(theme)
    )
    if style.reverse:
        foreground, background = background, foreground
    if style.dim:
        foreground = blend_rgb(foreground, background, _SVG_DIM_BLEND)
    return {"foreground": foreground.hex, "background": background.hex}


def _export_coordinates(
    segments: tuple[Segment, ...], width: int, settings: dict[str, Any]
) -> dict[str, Any]:
    """Observe the exporter's filtered/cropped track, never raw stream coordinates."""
    theme = settings.get("theme") or SVG_EXPORT_THEME
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    used = 0
    if len(segments) > MAX_SEGMENTS:
        return {"complete": False, "missing": ["segment_limit"], "segments": []}
    for y, line in enumerate(
        Segment.split_and_crop_lines(Segment.filter_control(segments), length=width)
    ):
        x = 0
        for segment in line:
            style = segment.style or Style()
            cells = cell_len(segment.text)
            record = {
                "x": x,
                "y": y,
                "cells": cells,
                "text": segment.text,
                "style": _style(style),
                "resolved": _resolved_colors(style, theme),
            }
            size = len(json.dumps(record, ensure_ascii=True).encode("utf-8"))
            if len(records) >= MAX_SEGMENTS or used + size > MAX_OBSERVATION_BYTES:
                return {
                    "complete": False,
                    "missing": ["export_coordinate_limit"],
                    "segments": records,
                }
            used += size
            records.append(record)
            x += cells
    return {
        "complete": True,
        "missing": missing,
        "segments": records,
        "console_width": width,
        "coordinates": "Segment.filter_control then Segment.split_and_crop_lines",
        "font_aspect_ratio": settings.get("font_aspect_ratio", 0.61),
        "template_sha256": sha256(str(settings.get("code_format", "")).encode()).hexdigest(),
        "theme": {
            "foreground": theme.foreground_color.hex,
            "background": theme.background_color.hex,
        },
    }


def _segments(console: Any, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    # Take the same recording lock as Rich, and release it before delegation.
    # Never clear, simplify, crop, or write to the original buffer.
    state = vars(console)
    with state["_record_buffer_lock"]:
        segments = tuple(state["_record_buffer"])
    rows = []
    x = y = used = 0
    missing = []
    for index, segment in enumerate(segments):
        if index >= MAX_SEGMENTS:
            missing.append("segment_limit")
            break
        parts = []
        for number, text in enumerate(segment.text.split("\n")):
            cells = cell_len(text)
            parts.append({"x": x, "y": y, "cells": cells, "text": text})
            x += cells
            if number < segment.text.count("\n"):
                x, y = 0, y + 1
        record = {
            "index": index,
            "text": segment.text,
            "control": repr(segment.control),
            "style": _style(segment.style),
            "stream_parts": parts,
        }
        size = len(json.dumps(record, ensure_ascii=True).encode("utf-8"))
        if used + size > MAX_OBSERVATION_BYTES:
            missing.append("segment_byte_limit")
            break
        used += size
        rows.append(record)
        if segment.control:
            missing.append("control_position_unresolved")
    return {
        "console_width": console.width,
        "recording": console.record,
        "segment_count": len(segments),
        "export_coordinates": _export_coordinates(segments, console.width, settings or {}),
        "segments": rows,
        "missing": sorted(set(missing)),
        "coordinates": (
            "record-buffer stream cells before exporter simplification; no assumed wrapping"
        ),
    }


@contextmanager
def observe_export() -> Any:
    """Temporarily observe the local recording Console created by export_screenshot."""
    original = Console.export_svg
    observation: dict[str, Any] = {
        "schema_version": 1,
        "invocations": [],
        "missing": [],
        "dependencies": dependency_sources(original),
    }

    def observed(console: Console, *args: Any, **kwargs: Any) -> str:
        invocation: dict[str, Any] = {
            "arguments": repr(args),
            "keyword_arguments": repr(kwargs),
        }
        observation["invocations"].append(invocation)
        try:
            bound = inspect.signature(original).bind(console, *args, **kwargs)
            bound.apply_defaults()
            invocation["recording"] = _segments(console, bound.arguments)
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as error:
            invocation["missing"] = "recording:" + type(error).__name__
        try:
            result = original(console, *args, **kwargs)
        except BaseException as error:
            invocation["export_exception"] = type(error).__name__
            raise
        invocation["raw_svg_sha256"] = sha256(result.encode("utf-8")).hexdigest()
        return result

    try:
        with patch.object(Console, "export_svg", observed):
            yield observation
    finally:
        if len(observation["invocations"]) != 1:
            observation["missing"].append("expected_one_recording_export")
        observation["complete"] = (
            not observation["missing"]
            and all(
                not item.get("missing")
                and not item.get("export_exception")
                and not item.get("recording", {}).get("missing")
                for item in observation["invocations"]
            )
            and all(
                not item.get("missing") and not item.get("module_missing")
                for item in observation["dependencies"]["sources"]
            )
        )


def _span_candidates(
    context: dict[str, Any], clip_id: str, part: dict[str, Any], width: float
) -> list[dict[str, Any]]:
    candidates = []
    for node in context["texts"]:
        chain = [node]
        while chain[-1] in context["parents"]:
            chain.append(context["parents"][chain[-1]])
        if not any(item.get("clip-path") == f"url(#{clip_id})" for item in chain):
            continue
        try:
            left = float(node.attrib["x"])
            extent = float(node.attrib["textLength"])
        except (KeyError, ValueError):
            continue
        if left < (part["x"] + part["cells"]) * width and left + extent > part["x"] * width:
            candidates.append({"text": node.text, "attributes": dict(node.attrib)})
    return candidates


def _map_part(context: dict[str, Any], part: dict[str, Any], console_width: int) -> dict[str, Any]:
    clips = context["clips"]
    matches = [key for key in clips if key.endswith(f"-line-{part['y']}")]
    if len(matches) != 1 or len(clips[matches[0]]) != 1 or console_width <= 0:
        return {"missing": "ambiguous_or_absent_line_clip"}
    clip_id = matches[0]
    width = float(clips[clip_id][0].attrib["width"]) / console_width
    if not math.isfinite(width) or width <= 0:
        return {"missing": "invalid_export_cell_width"}
    return {
        "line_clip": clip_id,
        "cell_width": width,
        "candidates": _span_candidates(context, clip_id, part, width),
        "qualification": (
            "overlap candidates only; no absence cause or resolved-style equivalence inferred"
        ),
    }


def map_export_spans(observation: dict[str, Any], svg: str) -> None:
    """Retain Rich line-clip candidates, leaving ambiguous mappings unresolved."""
    root = ET.fromstring(svg)  # noqa: S314 - same-invocation generated screenshot.
    context = {
        "parents": {child: parent for parent in root.iter() for child in parent},
        "clips": {
            node.attrib["id"]: node
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1] == "clipPath"
            and re.search(r"-line-\d+$", node.get("id", ""))
        },
        "texts": [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"text", "tspan"}],
    }
    for invocation in observation["invocations"]:
        recording = invocation.get("recording")
        if recording is None:
            continue
        mappings = []
        for segment in recording["segments"]:
            if not segment["style"]["flags"]["underline"]:
                continue
            for part in segment["stream_parts"]:
                if not part["cells"]:
                    continue
                mapping = {
                    "segment": segment["index"],
                    "stream_part": part,
                    **_map_part(context, part, recording["console_width"]),
                }
                if "missing" in mapping:
                    observation["complete"] = False
                    observation["missing"].append(mapping["missing"])
                mappings.append(mapping)
        invocation["underline_span_mapping"] = mappings
        track = recording.get("export_coordinates", {})
        invocation["export_underline_span_mapping"] = [
            {
                "export_part": segment,
                **_map_part(context, segment, recording["console_width"]),
            }
            for segment in track.get("segments", [])
            if segment["style"]["flags"]["underline"] and segment["cells"]
        ]


def bound_observation(observation: dict[str, Any]) -> dict[str, Any]:
    """Keep source/segment omissions explicit, including unsupported dependency APIs."""
    size = len(json.dumps(observation, ensure_ascii=True).encode("utf-8"))
    if size <= MAX_OBSERVATION_BYTES:
        return observation
    return {
        "schema_version": 1,
        "complete": False,
        "missing": ["observation_byte_limit"],
        "observed_byte_length": size,
        "byte_limit": MAX_OBSERVATION_BYTES,
        "invocation_count": len(observation.get("invocations", [])),
    }
