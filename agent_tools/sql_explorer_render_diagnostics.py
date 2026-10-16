"""Bounded, scene-specific evidence for the terminal to SVG rendering pipeline."""

from __future__ import annotations

import hashlib
import json
import platform
import re
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from xml.etree import ElementTree as ET

from rich.cells import cell_len
from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual.app import ScreenStackError
from textual.css.query import NoMatches
from textual.geometry import Region

# These limits apply across a scene, not independently to each widget.
MAX_REGIONS = 9
MAX_ROWS = 24
MAX_CELLS = 208
MAX_SVG_ELEMENTS = 256
MAX_BYTES = 512 * 1024
SCENES = frozenset(
    {
        "database-picker",
        "find-replace",
        "column-ruler-scrolled",
        "completion-columns",
        "create-table-advanced",
        "create-table-schema",
        "create-table-invalid",
        "create-table-sql",
    }
)
FLAGS = ("bold", "dim", "italic", "underline", "reverse", "strike", "conceal", "blink")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _color(color: Color | None, *, foreground: bool) -> str:
    color = color or Color.default()
    triplet = color.get_truecolor(foreground=foreground)
    return triplet.hex


def strip_record(strip: Any, *, x: int, y: int, width: int) -> dict[str, Any]:
    """Retain terminal cells, including wide and combining characters, not code points."""
    cropped = strip.crop(0, min(width, MAX_CELLS))
    spans = []
    offset = x
    for segment in cropped:
        if segment.control:
            continue
        style = segment.style or Style()
        cells = cell_len(segment.text)
        spans.append(
            {
                "text": segment.text,
                "x": offset,
                "cells": cells,
                "foreground": _color(style.color, foreground=True),
                "background": _color(style.bgcolor, foreground=False),
                "flags": {name: bool(getattr(style, name)) for name in FLAGS},
            }
        )
        offset += cells
    return {
        "x": x,
        "y": y,
        "cells": cropped.cell_length,
        "text": cropped.text,
        "spans": spans,
        "truncated": strip.cell_length > min(width, MAX_CELLS),
    }


def _region_record(region: Any) -> dict[str, int]:
    return {name: int(getattr(region, name)) for name in ("x", "y", "width", "height")}


SELECTOR_SCENES = frozenset({"create-table-schema", "create-table-invalid", "create-table-sql"})
_SELECTOR_STYLES = (
    "display",
    "visibility",
    "width",
    "height",
    "min_width",
    "min_height",
    "max_width",
    "max_height",
    "box_sizing",
    "padding",
    "border",
    "color",
    "background",
    "opacity",
    "text_opacity",
    "overflow_x",
    "overflow_y",
)


def _selector_child_state(widget: Any) -> dict[str, Any]:
    """Record computed child and ancestor geometry without changing layout."""
    ancestors = []
    parent = widget.parent
    for _depth in range(16):
        if parent is None or not hasattr(parent, "region"):
            parent = None
            break
        ancestors.append(
            {
                "class": type(parent).__name__,
                "id": parent.id,
                "region": _region_record(parent.region),
                "content_region": _region_record(parent.content_region),
                "clip": _region_record(parent.scrollable_content_region),
                "display": parent.display,
                "visible": parent.visible,
                "opacity": parent.styles.opacity,
                "text_opacity": parent.styles.text_opacity,
            }
        )
        parent = parent.parent
    return {
        "class": type(widget).__name__,
        "id": widget.id,
        "classes": sorted(widget.classes),
        "mounted": widget.is_mounted,
        "display": widget.display,
        "visible": widget.visible,
        "region": _region_record(widget.region),
        "content_region": _region_record(widget.content_region),
        "renderable_text": str(getattr(widget, "renderable", ""))[: MAX_CELLS * MAX_ROWS],
        "renderable_truncated": len(str(getattr(widget, "renderable", ""))) > MAX_CELLS * MAX_ROWS,
        "styles": {name: str(getattr(widget.styles, name)) for name in _SELECTOR_STYLES},
        "ancestors": ancestors,
        "ancestors_truncated": parent is not None,
    }


def _source_selector(app: Any, scene: str) -> tuple[dict[str, Any], list[tuple[str, Any, int]]]:
    """Every child query is rooted in the source Select, never the whole screen."""
    snapshot: dict[str, Any] = {
        "expected_value": "from_sql" if scene == "create-table-sql" else "table_schema",
        "children": {},
        "findings": [],
        "collection_missing": [],
    }
    targets = []
    try:
        selector = app.screen.query_one("#create-table-source")
    except (NoMatches, ScreenStackError, AttributeError, RuntimeError) as error:
        snapshot["collection_missing"].append(f"source-selector:{type(error).__name__}")
        snapshot["findings"].append("source-selector:missing")
        return snapshot, targets
    expanded = bool(selector.expanded)
    snapshot.update(
        selected_value=str(selector.value),
        allow_blank=selector._allow_blank,  # noqa: SLF001 - Textual stores this privately.
        expanded=expanded,
        active_arrow="up-arrow" if expanded else "down-arrow",
    )
    queries = {
        "source-selector": None,
        "source-current": "SelectCurrent",
        "source-label": "SelectCurrent > Static#label",
        "source-arrow": f"SelectCurrent > .{'up-arrow' if expanded else 'down-arrow'}",
        "source-inactive-arrow": f"SelectCurrent > .{'down-arrow' if expanded else 'up-arrow'}",
    }
    for name, query in queries.items():
        _selector_target(selector, name, query, snapshot, targets)
    return snapshot, targets


def _selector_target(
    selector: Any,
    name: str,
    query: str | None,
    snapshot: dict[str, Any],
    targets: list[tuple[str, Any, int]],
) -> None:
    try:
        widget = selector if query is None else selector.query_one(query)
    except NoMatches:
        snapshot["collection_missing"].append(f"{name}:missing_child")
        if name != "source-inactive-arrow":
            snapshot["findings"].append(f"{name}:missing_child")
        return
    snapshot["children"][name] = _selector_child_state(widget)
    if snapshot["children"][name]["renderable_truncated"]:
        snapshot["collection_missing"].append(f"{name}:renderable_limit")
    if snapshot["children"][name]["ancestors_truncated"]:
        snapshot["collection_missing"].append(f"{name}:ancestor_limit")
    if name != "source-inactive-arrow":
        targets.append((name, widget, max(1, widget.region.height)))


def _selector_visibility(snapshot: dict[str, Any], regions: list[dict[str, Any]]) -> None:
    """Observed visibility and evidence completeness deliberately have separate verdicts."""
    findings = snapshot["findings"]
    if snapshot.get("selected_value") != snapshot["expected_value"]:
        findings.append("source-selector:unexpected_value")
    by_name = {entry["name"]: entry for entry in regions}
    for name, state in snapshot["children"].items():
        if name == "source-inactive-arrow":
            continue  # Textual intentionally hides the opposite arrow.
        _selector_geometry_findings(name, state, findings)
        if name in {"source-label", "source-arrow"}:
            expected = (
                snapshot["expected_value"]
                if name == "source-label"
                else ("▲" if snapshot.get("expanded") else "▼")
            )
            _selector_text_findings(name, by_name.get(name, {}), expected, findings)
    snapshot["findings"] = sorted(set(findings))
    snapshot["visibility_ok"] = not snapshot["findings"]


def _selector_geometry_findings(name: str, state: dict[str, Any], findings: list[str]) -> None:
    if not state["mounted"] or not state["display"] or not state["visible"]:
        findings.append(f"{name}:hidden_or_unmounted")
    if state["styles"]["opacity"] in {"0", "0.0"} or state["styles"]["text_opacity"] in {
        "0",
        "0.0",
    }:
        findings.append(f"{name}:transparent_output")
    if any(
        not ancestor["display"]
        or not ancestor["visible"]
        or ancestor["opacity"] == 0
        or ancestor["text_opacity"] == 0
        for ancestor in state["ancestors"]
    ):
        findings.append(f"{name}:concealed_by_ancestor")
    content = state["content_region"]
    if content["width"] <= 0 or content["height"] <= 0:
        findings.append(f"{name}:empty_content_region")
    region = state["region"]
    for ancestor in state["ancestors"]:
        clip = ancestor["clip"]
        if (
            region["x"] < clip["x"]
            or region["y"] < clip["y"]
            or region["x"] + region["width"] > clip["x"] + clip["width"]
            or region["y"] + region["height"] > clip["y"] + clip["height"]
        ):
            findings.append(f"{name}:ancestor_clipping")
            break


def _selector_text_findings(
    name: str, entry: dict[str, Any], expected: str, findings: list[str]
) -> None:
    for layer in ("widget_rows", "composed_rows"):
        rows = entry.get(layer, [])
        if expected not in "\n".join(row["text"] for row in rows):
            findings.append(f"{name}:{layer}:absent_expected_text")
        readable = "".join(
            span["text"]
            for row in rows
            for span in row["spans"]
            if not span["flags"]["conceal"] and span["foreground"] != span["background"]
        )
        if expected not in readable:
            findings.append(f"{name}:{layer}:concealed_output")


def selector_readiness_snapshot(
    app: Any, scene: str, *, terminal: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Collect observations even when selector visibility blocks production capture."""
    if terminal is None:
        if scene not in SELECTOR_SCENES:
            return {}
        terminal = terminal_diagnostics(app, scene)
    bounded = bounded_payload(
        {
            "complete": not terminal["missing"] and not terminal["omissions"],
            "missing": terminal["missing"][:],
            "omissions": terminal["omissions"][:],
            "terminal": {**terminal, "regions": terminal["regions"][:]},
            "svg": {"elements": [], "omitted_elements": 0},
        }
    )
    return {
        "selector": bounded.get("terminal", {}).get("source_selector", {}),
        "regions": [
            entry
            for entry in bounded.get("terminal", {}).get("regions", [])
            if entry.get("name", "").startswith("source-")
        ],
        "collection_complete": bounded["complete"],
        "collection_missing": bounded["missing"],
        "omissions": bounded["omissions"],
    }


def selector_readiness_assertions(snapshot: dict[str, Any]) -> dict[str, bool]:
    """Require complete evidence and readable children within every ancestor clip."""
    selector = snapshot.get("selector", {})
    expected = selector.get("expected_value")
    return {
        "source_selector_evidence_complete": snapshot.get("collection_complete") is True,
        "source_selector_expected_value": (
            expected is not None and selector.get("selected_value") == expected
        ),
        "source_selector_visible": selector.get("visibility_ok") is True,
    }


def _targets(app: Any, scene: str) -> list[tuple[str, Any, int]]:
    if scene == "database-picker":
        return [("heading", app.screen.query_one("#database-picker-title"), 3)]
    if scene == "find-replace":
        return [("find-matches", app.active_workspace.editor, 6)]
    if scene == "completion-columns":
        return [
            ("completion-anchor", app.active_workspace.editor, 3),
            ("completion-popup", app.active_workspace.completion_menu, 5),
        ]
    if scene == "column-ruler-scrolled":
        editor = app.active_workspace.editor
        return [
            ("ruler", app.active_workspace.query_one("#column-ruler"), 1),
            ("horizontal-scrollbar", editor.horizontal_scrollbar, 1),
        ]
    targets = [
        (name, app.screen.query_one(f"#create-{name}"), 1)
        for name in ("insert_data", "skip_if_exists", "drop_if_exists")
    ]
    viewport = app.screen.query_one("#create-table-fields").scrollable_content_region
    targets.extend(
        (f"remove-column-{index}", widget, 1)
        for index, widget in enumerate(app.screen.query(".remove-column"))
        if widget.display
        and widget.region.width
        and widget.region.height
        and widget.region.y < viewport.bottom
        and widget.region.bottom > viewport.y
    )
    return targets


def _widget_state(widget: Any) -> dict[str, Any]:
    state = {
        "class": type(widget).__name__,
        "id": widget.id,
        "enabled": not widget.disabled,
        "focused": widget.has_focus,
        "display": widget.display,
    }
    if hasattr(widget, "label"):
        label = str(widget.label)
        state["label"] = label[: MAX_CELLS * MAX_ROWS]
        state["label_truncated"] = len(label) > MAX_CELLS * MAX_ROWS
    if hasattr(widget, "value") and isinstance(widget.value, bool):
        state["value"] = widget.value
        state["button_text"] = widget._render_button().plain  # noqa: SLF001
    return state


def _checkbox_context(app: Any, result: dict[str, Any]) -> None:
    try:
        advanced = app.screen.query_one("#create-table-advanced")
        result["checkbox_context"] = {"advanced_collapsed": bool(advanced.collapsed)}
    except (NoMatches, ScreenStackError, AttributeError, RuntimeError) as error:
        result["missing"].append(f"checkbox_context:{type(error).__name__}")


def _checkbox_evidence(widget: Any, entry: dict[str, Any], result: dict[str, Any]) -> None:
    """Observe model/direct output independently of cached and composed rows."""
    name = entry["name"]
    try:
        geometry = _selector_child_state(widget)
        entry["checkbox"] = {"geometry": geometry}
        if geometry["ancestors_truncated"]:
            result["omissions"].append(f"{name}:ancestor_limit")
        if entry["state"]["label_truncated"]:
            result["omissions"].append(f"{name}:label_limit")
        rendered = widget.render()
        if not isinstance(rendered, Text):
            result["missing"].append(f"{name}:unsupported_direct_render")
            return
        limit = MAX_CELLS * MAX_ROWS
        truncated = len(rendered.plain) > limit or len(rendered.spans) > MAX_SVG_ELEMENTS
        entry["checkbox"]["direct_render"] = {
            "text": rendered.plain[:limit],
            "style": str(rendered.style),
            "spans": [
                {"start": span.start, "end": span.end, "style": str(span.style)}
                for span in rendered.spans[:MAX_SVG_ELEMENTS]
            ],
            "truncated": truncated,
        }
        if truncated:
            result["omissions"].append(f"{name}:direct_render_limit")
    except (AttributeError, RuntimeError) as error:
        result["missing"].append(f"{name}:checkbox:{type(error).__name__}")


def checkbox_failure_snapshot(app: Any, scene: str, *, terminal: dict[str, Any]) -> dict[str, Any]:
    """Bound the original failing observation; never refresh or recollect its rows."""
    payload = bounded_payload(
        {
            "schema_version": 1,
            "scene_id": scene,
            "complete": not terminal["missing"] and not terminal["omissions"],
            "missing": terminal["missing"][:],
            "omissions": terminal["omissions"][:],
            "versions": {
                "python": platform.python_version(),
                **{package: _package_version(package) for package in ("textual", "rich")},
            },
            "terminal_size": [app.size.width, app.size.height],
            "terminal": {**terminal, "regions": terminal["regions"][:]},
            "svg": {"elements": [], "omitted_elements": 0},
        }
    )
    payload.pop("svg", None)
    return payload


def _collect_region_rows(
    widget: Any,
    composed: Any,
    entry: dict[str, Any],
    result: dict[str, Any],
    count: int,
) -> None:
    """Record one bounded region and retain failures without aborting later regions."""
    name, region = entry["name"], widget.region
    width = min(region.width, MAX_CELLS)
    try:
        local = widget.render_lines(Region(0, 0, width, count))
        for row in range(count):
            entry["widget_rows"].append(
                strip_record(local[row], x=region.x, y=region.y + row, width=width)
            )
            if not 0 <= region.y + row < len(composed) or region.x < 0:
                result["missing"].append(f"{name}:outside_screen")
                continue
            screen_row = composed[region.y + row]
            clipped = screen_row.crop(region.x, region.x + width)
            entry["composed_rows"].append(
                strip_record(clipped, x=region.x, y=region.y + row, width=width)
            )
            if any(
                rows[-1]["truncated"] for rows in (entry["widget_rows"], entry["composed_rows"])
            ):
                result["omissions"].append(f"{name}:row_truncated")
            if clipped.cell_length < width:
                result["missing"].append(f"{name}:incomplete_screen_row")
    except (AttributeError, IndexError, RuntimeError) as error:
        result["missing"].append(f"{name}:{type(error).__name__}")


def _editor_diagnostics(app: Any, scene: str, result: dict[str, Any]) -> None:
    """Attach caret, scrolling, and popup geometry only for their assigned scenes."""
    if scene in {"find-replace", "column-ruler-scrolled"}:
        editor = app.active_workspace.editor
        result["editor"] = {
            "scroll_offset": list(editor.scroll_offset),
            "scroll_range": [editor.max_scroll_x, editor.max_scroll_y],
            "viewport": _region_record(editor.scrollable_content_region),
            "content_size": list(editor.virtual_size),
            "selections": [
                [list(selection.start), list(selection.end)]
                for selection in editor.cursor_selections
            ],
            "carets": [list(selection.end) for selection in editor.cursor_selections],
        }
        if scene == "find-replace":
            result["editor"]["matches"] = [
                [list(start), list(end)]
                for start, end in editor._search_matches  # noqa: SLF001
            ]
        else:
            scrollbar = editor.horizontal_scrollbar
            result["editor"]["scrollbar"] = {
                key: getattr(scrollbar, key)
                for key in ("position", "window_size", "window_virtual_size")
            }
    if scene == "completion-columns":
        editor = app.active_workspace.editor
        _, cursor_y = editor.wrapped_document.location_to_offset(editor.cursor_location)
        result["completion"] = {
            "active_row": editor.scrollable_content_region.y + cursor_y - int(editor.scroll_y),
            "viewport": _region_record(editor.scrollable_content_region),
            "popup": _region_record(app.active_workspace.completion_menu.region),
        }


def _collect_target_region(
    target: tuple[str, Any, int],
    composed: Any,
    result: dict[str, Any],
    row_budget: int,
) -> int:
    """Collect one target while preserving the shared scene row budget."""
    name, widget, requested_rows = target
    region = widget.region
    entry: dict[str, Any] = {
        "name": name,
        "region": _region_record(region),
        "state": _widget_state(widget),
        "widget_rows": [],
        "composed_rows": [],
    }
    if name == "heading":
        entry["state"]["renderable_text"] = str(widget.render())
    result["regions"].append(entry)
    if isinstance(entry["state"].get("value"), bool):
        _checkbox_evidence(widget, entry, result)
    if not widget.display or region.width <= 0 or region.height <= 0:
        if not name.startswith("source-"):
            result["missing"].append(f"{name}:not_visible")
        return row_budget
    count = min(requested_rows, region.height, row_budget)
    if count < requested_rows:
        result["missing"].append(f"{name}:rows")
    row_budget -= count
    _collect_region_rows(widget, composed, entry, result, count)
    if region.width > MAX_CELLS:
        result["omissions"].append(f"{name}:cell_limit")
    return row_budget


def terminal_diagnostics(app: Any, scene: str) -> dict[str, Any]:
    """Separate the widget render cache from the screen compositor's actual strips."""
    result: dict[str, Any] = {"regions": [], "missing": [], "omissions": []}
    selector_targets = []
    if scene.startswith("create-table"):
        _checkbox_context(app, result)
    if scene in SELECTOR_SCENES:
        snapshot, selector_targets = _source_selector(app, scene)
        result["source_selector"] = snapshot
        result["missing"].extend(snapshot["collection_missing"])
    try:
        targets = _targets(app, scene)
        composed = app.screen._compositor.render_strips()  # noqa: SLF001
    except (AttributeError, LookupError, NoMatches, ScreenStackError, RuntimeError) as error:
        result["missing"].append(f"terminal:{type(error).__name__}")
        if "source_selector" in result:
            _selector_visibility(result["source_selector"], [])
        return result
    targets.extend(selector_targets)
    if len(targets) > MAX_REGIONS:
        result["omissions"].append("region_limit")
        result["omissions"].extend(f"{name}:region_limit" for name, *_rest in targets[MAX_REGIONS:])
    row_budget = MAX_ROWS
    for target in targets[:MAX_REGIONS]:
        row_budget = _collect_target_region(target, composed, result, row_budget)
    _editor_diagnostics(app, scene, result)
    if "source_selector" in result:
        _selector_visibility(result["source_selector"], result["regions"])
    if not result["regions"]:
        result["missing"].append("terminal:required_regions")
    return result


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _declarations(text: str) -> dict[str, str]:
    return {
        key.strip(): value.strip()
        for item in text.split(";")
        if ":" in item
        for key, value in [item.split(":", 1)]
    }


def svg_diagnostics(  # noqa: C901, PLR0912, PLR0915
    svg: str, terminal: dict[str, Any]
) -> dict[str, Any]:
    """Use Rich's exported line clips, retaining mapping failures rather than guessing."""
    root = ET.fromstring(svg)  # noqa: S314 - produced by the headless app.
    rows = {row["y"] for region in terminal["regions"] for row in region["composed_rows"]}
    nodes = list(root.iter())
    parents = {child: parent for parent in nodes for child in parent}
    css: dict[str, dict[str, str]] = {}
    for node in nodes:
        if _tag(node) == "style":
            for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", node.text or ""):
                css[selector.strip()] = _declarations(declarations)
    clips: dict[str, ET.Element] = {}
    selected_clips: set[str] = set()
    intervals: list[tuple[float, float]] = []
    for node in nodes:
        if _tag(node) != "clipPath":
            continue
        clip_id = node.get("id", "")
        clips[clip_id] = node
        match = re.search(r"-line-(\d+)$", clip_id)
        if match and int(match[1]) in rows:
            selected_clips.add(clip_id)
            intervals.extend(
                (float(rect.get("y", "0")), float(rect.get("height", "0")))
                for rect in node
                if _tag(rect) == "rect"
            )
    selected = set()
    for node in nodes:
        reference = node.get("clip-path", "")
        references = [reference]
        ancestor = parents.get(node)
        while ancestor is not None:
            references.append(ancestor.get("clip-path", ""))
            ancestor = parents.get(ancestor)
        in_clip = any(f"url(#{clip_id})" in references for clip_id in selected_clips)
        in_row = False
        if _tag(node) == "rect" and "y" in node.attrib:
            y = float(node.get("y", "0"))
            rect_height = float(node.get("height", "0"))
            in_row = any(y < top + height and y + rect_height > top for top, height in intervals)
        if in_clip or in_row:
            selected.add(node)
    # Clip definitions and ancestors retain coordinate systems and inherited styles.
    for clip_id in selected_clips:
        selected.update(clips[clip_id].iter())
    for node in list(selected):
        parent = parents.get(node)
        while parent is not None:
            selected.add(parent)
            parent = parents.get(parent)
    # Preserve every referenced clipping definition, including the outer screen clip.
    clip_references = {
        clip_id
        for node in selected
        for clip_id in clips
        if node.get("clip-path") == f"url(#{clip_id})"
    }
    for clip_id in clip_references:
        selected.update(clips[clip_id].iter())
    output = []
    for node in nodes:
        if node not in selected:
            continue
        chain = []
        parent = parents.get(node)
        while parent is not None:
            chain.append(parent)
            parent = parents.get(parent)
        effective: dict[str, str] = {}
        for ancestor in [*reversed(chain), node]:
            effective.update(
                {
                    key: value
                    for key, value in ancestor.attrib.items()
                    if key
                    in {
                        "fill",
                        "stroke",
                        "opacity",
                        "font-family",
                        "font-size",
                        "font-weight",
                        "visibility",
                    }
                }
            )
            for name in ancestor.get("class", "").split():
                effective.update(css.get(f".{name}", {}))
            effective.update(_declarations(ancestor.get("style", "")))
        output.append(
            {
                "tag": _tag(node),
                "attributes": dict(node.attrib),
                "text": node.text if _tag(node) in {"text", "tspan"} else None,
                "effective_style": effective,
                "ancestor_transforms": [
                    ancestor.get("transform")
                    for ancestor in reversed(chain)
                    if ancestor.get("transform")
                ],
                "ancestor_clips": [
                    ancestor.get("clip-path")
                    for ancestor in reversed(chain)
                    if ancestor.get("clip-path")
                ],
            }
        )
    return {
        "viewBox": root.get("viewBox"),
        "root_attributes": dict(root.attrib),
        "line_clip_ids": sorted(selected_clips),
        "elements": output[:MAX_SVG_ELEMENTS],
        "omitted_elements": max(0, len(output) - MAX_SVG_ELEMENTS),
        "missing": [
            f"svg:line_clip_mapping:{row}"
            for row in sorted(rows)
            if not any(clip_id.endswith(f"-line-{row}") for clip_id in selected_clips)
        ],
        "style_resolution": "presentation attributes, Rich class rules, inline styles",
    }


def bounded_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Fail visibly if evidence is too large; never silently treat truncation as complete."""
    while (
        len(json.dumps(payload, indent=4, sort_keys=True, ensure_ascii=True).encode("utf-8"))
        > MAX_BYTES
    ):
        payload["complete"] = False
        payload["omissions"].append("byte_limit")
        if payload["svg"]["elements"]:
            payload["svg"]["elements"].pop()
            payload["svg"]["omitted_elements"] += 1
        elif payload["terminal"]["regions"]:
            payload["terminal"]["regions"].pop()
        else:
            return {
                "schema_version": 1,
                "complete": False,
                "missing": ["payload_exceeds_byte_limit"],
                "omissions": ["byte_limit"],
            }
        payload["omissions"] = sorted(set(payload["omissions"]))
    return payload


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unavailable"


def render_diagnostics(  # noqa: PLR0913
    app: Any, scene: str, svg: str, framed_svg: str, png: bytes, terminal: dict[str, Any]
) -> dict[str, Any]:
    versions = {"python": platform.python_version()}
    for package in ("textual", "rich", "resvg-py"):
        versions[package] = _package_version(package)
    export = svg_diagnostics(svg, terminal)
    frame = ET.fromstring(framed_svg)  # noqa: S314 - generated by the capture harness.
    export["frame_attributes"] = dict(frame.attrib)
    export["framed_screen_attributes"] = [
        dict(child.attrib) for child in frame if _tag(child) == "svg"
    ]
    missing = terminal["missing"] + export["missing"]
    omissions = terminal["omissions"][:]
    if export["omitted_elements"]:
        omissions.append("svg_element_limit")
    return bounded_payload(
        {
            "schema_version": 1,
            "scene_id": scene,
            "complete": not missing and not omissions,
            "missing": missing,
            "omissions": omissions,
            "hashes": {
                "raw_svg_sha256": digest(svg.encode("utf-8")),
                "framed_svg_sha256": digest(framed_svg.encode("utf-8")),
                "png_sha256": digest(png),
            },
            "terminal_size": [app.size.width, app.size.height],
            "color_system": app.console.color_system,
            "versions": versions,
            "terminal": terminal,
            "svg": export,
        }
    )
