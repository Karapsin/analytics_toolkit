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
MAX_LIFECYCLE_BYTES = 8192
BORDER_ROWS_PER_REGION = 9
BORDER_SCENES = frozenset({"connections-searching", "result-success"})
SCENES = frozenset(
    {
        "database-picker",
        "find-replace",
        "column-ruler-scrolled",
        "completion-columns",
        "keyboard-multicursor",
        "editor-command-caret",
        "new-file",
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
        targets = [("heading", app.screen.query_one("#database-picker-title"), 3)]
    elif scene == "connections-searching":
        targets = [
            (
                "connections-border",
                app.screen.query_one("#connections-picker"),
                BORDER_ROWS_PER_REGION,
            ),
        ]
    elif scene == "result-success":
        targets = [
            (name, app.active_workspace.query_one(f".{name}"), BORDER_ROWS_PER_REGION)
            for name in ("query-pane", "result-pane")
        ]
    elif scene == "find-replace":
        targets = [("find-matches", app.active_workspace.editor, 6)]
    elif scene in {"keyboard-multicursor", "editor-command-caret"}:
        targets = [
            ("multicursor-editor", app.active_workspace.editor, 3),
            ("multicursor-ruler", app.active_workspace.query_one("#column-ruler"), 1),
        ]
    elif scene == "new-file":
        dialog = app.screen.query_one("#new-file-dialog")
        targets = [
            ("new-file-dialog", dialog, max(1, dialog.region.height)),
            ("new-file-name", app.screen.query_one("#new-file-name"), 3),
        ]
    elif scene == "completion-columns":
        targets = [
            ("completion-anchor", app.active_workspace.editor, 3),
            ("completion-popup", app.active_workspace.completion_menu, 5),
        ]
    elif scene == "column-ruler-scrolled":
        editor = app.active_workspace.editor
        targets = [
            ("ruler", app.active_workspace.query_one("#column-ruler"), 1),
            ("horizontal-scrollbar", editor.horizontal_scrollbar, 1),
        ]
    else:
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


def _crop_caret_span(
    span: dict[str, Any], left: int, right: int, *, retained: bool
) -> tuple[dict[str, Any] | None, bool]:
    """Keep zero-width suffixes with their retained base, even across style spans."""
    position = span["x"]
    text = []
    cells = 0
    for character in span["text"]:
        size = cell_len(character)
        if not size:
            if retained:
                text.append(character)
            continue
        start, end = max(left, position), min(right, position + size)
        retained = left <= position and position + size <= right
        if start < end:
            text.append(character if retained else " " * (end - start))
            cells += end - start
        position += size
    if not text:
        return None, retained
    cropped = {
        **span,
        "x": max(left, span["x"]),
        "text": "".join(text),
        "cells": cells,
    }
    return cropped, retained


def _caret_cells(
    rows: list[dict[str, Any]], *, x: int, y: int, width: int
) -> dict[str, Any] | None:
    """Crop already collected terminal cells; never render or refresh a second time."""
    row = next((item for item in rows if item["y"] == y), None)
    if row is None:
        return None
    spans = []
    retained = False
    for span in row["spans"]:
        cropped, retained = _crop_caret_span(span, x, x + width, retained=retained)
        if cropped is not None:
            spans.append(cropped)
    cells = sum(span["cells"] for span in spans)
    return {
        "x": x,
        "y": y,
        "cells": cells,
        "text": "".join(span["text"] for span in spans),
        "spans": spans,
        "truncated": cells != width or row["truncated"],
    }


def _caret_mapping(editor: Any, selection: Any) -> dict[str, Any]:
    row, column = selection.end
    wrapped_x, wrapped_y = editor.wrapped_document.location_to_offset(selection.end)
    x = wrapped_x - int(editor.scroll_x) + editor.gutter_width
    y = wrapped_y - int(editor.scroll_y)
    in_document = column < len(editor.document[row])
    width = 1
    if in_document:
        next_x, next_y = editor.wrapped_document.location_to_offset((row, column + 1))
        width = (
            max(1, next_x - wrapped_x)
            if next_y == wrapped_y
            else max(1, editor.scrollable_content_region.width - x)
        )
    origin = editor.content_region
    screen_x, screen_y = origin.x + x, origin.y + y
    viewport = editor.scrollable_content_region
    visible = (
        x >= editor.gutter_width
        and viewport.x <= screen_x
        and screen_x + width <= viewport.right
        and viewport.y <= screen_y < viewport.bottom
    )
    return {
        "wrapped_offset": [wrapped_x, wrapped_y],
        "content_local_cell": [x, y],
        "widget_local_cell": [screen_x - editor.region.x, screen_y - editor.region.y],
        "screen_cell": [screen_x, screen_y],
        "cell_width": width,
        "in_document": in_document,
        "visible": visible,
        "clipped": not visible,
    }


def _observe_caret_layers(
    caret: dict[str, Any], regions: list[dict[str, Any]], result: dict[str, Any]
) -> None:
    name = f"multicursor:caret:{caret['index']}"
    if caret["clipped"]:
        result["missing"].append(f"{name}:clipped")
    region = next((entry for entry in regions if entry["name"] == "multicursor-editor"), {})
    x, y = caret["screen_cell"]
    width = min(caret["cell_width"], MAX_CELLS)
    if width < caret["cell_width"]:
        result["omissions"].append(f"{name}:cell_limit")
    for layer in ("widget_rows", "composed_rows"):
        observed = _caret_cells(region.get(layer, []), x=x, y=y, width=width)
        caret[layer] = observed
        if observed is None or observed["truncated"]:
            result["missing"].append(f"{name}:{layer}:unavailable_or_clipped")
            continue
        # A recorded style is not a PNG visibility verdict.
        marked = (
            all(span["flags"]["underline"] for span in observed["spans"])
            if caret["in_document"]
            else observed["text"] == "│"
        )
        if not marked:
            result["multicursor"]["findings"].append(f"{name}:{layer}:caret_mark_absent")


def _collect_caret(editor: Any, selection: Any, index: int, result: dict[str, Any]) -> None:
    caret = {
        "index": index,
        "identity": id(selection),
        "primary": selection is editor.selection,
        "start": list(selection.start),
        "end": list(selection.end),
        "empty": selection.is_empty,
        "preferred_column": editor._cursor_preferences.get(selection),  # noqa: SLF001
    }
    result["multicursor"]["carets"].append(caret)
    try:
        caret.update(_caret_mapping(editor, selection))
        _observe_caret_layers(caret, result["regions"], result)
    except (AttributeError, LookupError, RuntimeError, TypeError, ValueError) as error:
        result["missing"].append(f"multicursor:caret:{index}:{type(error).__name__}")


def _multicursor_diagnostics(
    app: Any, result: dict[str, Any], *, scene: str = "keyboard-multicursor"
) -> None:
    """Observe actual state separately from fixture expectations without changing either."""
    snapshot: dict[str, Any] = {
        "expected": {
            "positions": [[0, 14], [1, 14], [2, 14]],
            "primary": [1, 14],
            "empty_selections": True,
            "keyboard_mode": True,
        },
        "carets": [],
        "findings": [],
    }
    if scene == "editor-command-caret":
        snapshot["expected"].update(
            positions=[[1, 14]], keyboard_mode=False, focused_widget_id="command-input"
        )
    result["multicursor"] = snapshot
    try:
        workspace = app.active_workspace
        editor = workspace.editor
        selections = editor.cursor_selections
        focused = app.focused
        snapshot.update(
            active_workspace=workspace.tab_id,
            keyboard_mode=app.keyboard_mode,
            focused_widget=(
                {
                    "class": type(focused).__name__,
                    "id": focused.id,
                    "identity": id(focused),
                }
                if focused is not None
                else None
            ),
            editor_focused=editor.has_focus,
            cursor_count=editor.cursor_count,
            primary_identity=id(editor.selection),
            scroll_offset=list(editor.scroll_offset),
            gutter_width=editor.gutter_width,
            gutter_left=editor.gutter.left,
            region=_region_record(editor.region),
            content_region=_region_record(editor.content_region),
            viewport=_region_record(editor.scrollable_content_region),
            preference_count=len(editor._cursor_preferences),  # noqa: SLF001
        )
        if len(selections) > MAX_REGIONS:
            result["omissions"].append("multicursor:caret_limit")
        for index, selection in enumerate(selections[:MAX_REGIONS]):
            _collect_caret(editor, selection, index, result)
        expected = snapshot["expected"]
        if [list(selection.end) for selection in selections[:MAX_REGIONS]] != expected["positions"]:
            snapshot["findings"].append("multicursor:unexpected_positions")
        if list(editor.selection.end) != expected["primary"]:
            snapshot["findings"].append("multicursor:unexpected_primary")
        if not all(selection.is_empty for selection in selections[:MAX_REGIONS]):
            snapshot["findings"].append("multicursor:nonempty_selection")
        if snapshot["keyboard_mode"] is not expected["keyboard_mode"]:
            snapshot["findings"].append("multicursor:unexpected_keyboard_mode")
    except (AttributeError, NoMatches, ScreenStackError, RuntimeError) as error:
        result["missing"].append(f"multicursor:state:{type(error).__name__}")


def _observed_attribute(widget: Any, name: str) -> Any:
    """Return unavailable probes explicitly rather than substituting expected state."""
    try:
        value = getattr(widget, name)
    except (AttributeError, RuntimeError) as error:
        return {"unavailable": type(error).__name__}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    if len(text) > MAX_CELLS:
        return {"value": text[:MAX_CELLS], "truncated": True}
    return text


def _layout_observation(widget: Any) -> dict[str, Any]:
    values = {
        "class": type(widget).__name__,
        "id": widget.id,
        "mounted": widget.is_mounted,
        "region": _region_record(widget.region),
        "content_region": _region_record(widget.content_region),
        "viewport": _region_record(widget.scrollable_content_region),
        "scroll_offset": list(widget.scroll_offset),
        "pending": {
            name: _observed_attribute(widget, name)
            for name in ("_layout_required", "_repaint_required", "_scroll_required")
        },
        "styles": {
            name: _observed_attribute(widget.styles, name)
            for name in (
                "align_horizontal",
                "align_vertical",
                "content_align_horizontal",
                "content_align_vertical",
                "layout",
                "width",
                "height",
                "offset",
                "dock",
            )
        },
    }
    try:
        queue = widget._message_queue  # noqa: SLF001 - observe only; never drain messages.
        values["queued_messages"] = queue.qsize()
    except (AttributeError, RuntimeError) as error:
        values["queued_messages"] = {"unavailable": type(error).__name__}
    return values


def styled_content_row(widget: Any) -> dict[str, Any]:
    """Observe the first content row through Textual's native styles cache."""
    try:
        content, outer = widget.content_region, widget.region
        clip = Region(
            content.x - outer.x,
            content.y - outer.y,
            min(content.width, MAX_CELLS),
            1,
        )
        strips = widget.render_lines(clip)
        strip = strips[0]
        row = strip_record(strip, x=content.x, y=content.y, width=content.width)
        observation = ruler_strip_observation(strip, row, width=content.width)
        omissions = list(observation["omissions"])
        if content.width > MAX_CELLS:
            omissions.append("styled_widget:cell_limit")
        if row["cells"] != clip.width:
            omissions.append("styled_widget:row_width")
        snapshot = {
            "complete": observation["complete"] and not omissions and not row["truncated"],
            "row": row,
            "widget_local_clip": _region_record(clip),
            "strip_observation": observation,
            "omissions": omissions,
        }
    except (AttributeError, IndexError, RuntimeError, TypeError, ValueError) as error:
        return {
            "complete": False,
            "row": None,
            "missing": [f"styled_widget:{type(error).__name__}"],
            "omissions": [],
        }
    return snapshot


def _styled_characters(row: dict[str, Any]) -> list[tuple[Any, ...]]:
    characters = []
    for span in row["spans"]:
        offset = span["x"]
        style = (
            span["foreground"],
            span["background"],
            tuple(span["flags"][flag] for flag in FLAGS),
        )
        for character in span["text"]:
            characters.append((character, offset, *style))
            offset += cell_len(character)
    return characters


def rendered_rows_equal(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    """Compare complete positioned text and resolved styles, ignoring segment splits."""
    if left is None or right is None or left["truncated"] or right["truncated"]:
        return False
    return all(left[key] == right[key] for key in ("x", "y", "cells", "text")) and (
        _styled_characters(left) == _styled_characters(right)
    )


def ruler_strip_observation(strip: Any, row: dict[str, Any], *, width: int) -> dict[str, Any]:
    """Describe the already collected input and crop; never render or pad a strip."""
    try:
        snapshot = {
            "complete": True,
            "omissions": [],
            "original_lengths": {"characters": len(strip.text), "cells": strip.cell_length},
            "requested_crop_width": width,
            "effective_crop_width": min(width, MAX_CELLS),
            "result_lengths": {"characters": len(row["text"]), "cells": row["cells"]},
            "segments": [
                {"text": segment.text, "style": str(segment.style), "control": str(segment.control)}
                for segment in strip
            ],
        }
        if len(json.dumps(snapshot, ensure_ascii=True).encode("utf-8")) > MAX_LIFECYCLE_BYTES:
            snapshot.pop("segments")
            snapshot.update(complete=False, omissions=["strip_observation_byte_limit"])
    except (AttributeError, RuntimeError, TypeError, ValueError) as error:
        return {
            "complete": False,
            "missing": [f"strip_observation:{type(error).__name__}"],
            "omissions": [],
        }
    return snapshot


def ruler_lifecycle_snapshot(app: Any, rows: dict[str, Any]) -> dict[str, Any]:
    """Observe the failing ruler frame without rendering rows or changing lifecycle."""
    workspace = app.active_workspace
    widgets = {
        "ruler": workspace.query_one("#column-ruler"),
        "editor": workspace.editor,
        "command": workspace.command_input,
    }
    snapshot = {
        "focused_widget": {
            "class": type(app.focused).__name__,
            "id": getattr(app.focused, "id", None),
            "identity": id(app.focused),
        },
        "widgets": {name: _layout_observation(widget) for name, widget in widgets.items()},
        "editor_mapping": {
            name: _observed_attribute(workspace.editor, name)
            for name in (
                "gutter_width",
                "gutter",
                "show_horizontal_scrollbar",
                "show_vertical_scrollbar",
            )
        },
        "row_lengths": {
            name: {
                layer: {"characters": len(row["text"]), "cells": row["cells"]}
                for layer, row in entry.items()
                if layer in {"widget_row", "compositor_row"}
            }
            for name, entry in rows.items()
            if name in {"ruler", "editor"}
        },
        "versions": {
            "python": platform.python_version(),
            **{package: _package_version(package) for package in ("textual", "rich")},
        },
    }
    if len(json.dumps(snapshot, ensure_ascii=True).encode("utf-8")) > MAX_LIFECYCLE_BYTES:
        return {"complete": False, "omissions": ["lifecycle_byte_limit"]}
    return snapshot


def _new_file_diagnostics(app: Any, result: dict[str, Any]) -> None:
    try:
        screen = app.screen
        dialog = screen.query_one("#new-file-dialog")
        result["new_file"] = {
            "screen": _layout_observation(screen),
            "screen_mro": [
                f"{cls.__module__}.{cls.__name__}" for cls in type(screen).__mro__[:MAX_REGIONS]
            ],
            "screen_mro_truncated": len(type(screen).__mro__) > MAX_REGIONS,
            "dialog": _layout_observation(dialog),
            "dialog_geometry": _selector_child_state(dialog),
        }
        if result["new_file"]["screen_mro_truncated"]:
            result["omissions"].append("new-file:screen_mro_limit")
        if result["new_file"]["dialog_geometry"]["ancestors_truncated"]:
            result["omissions"].append("new-file:ancestor_limit")
    except (AttributeError, LookupError, NoMatches, ScreenStackError, RuntimeError) as error:
        result["missing"].append(f"new-file:state:{type(error).__name__}")


def _scene_state_diagnostics(app: Any, scene: str, result: dict[str, Any]) -> None:
    """Keep actual state observable even when the compositor is unavailable."""
    if scene in {"keyboard-multicursor", "editor-command-caret"}:
        _multicursor_diagnostics(app, result, scene=scene)
    if scene == "new-file":
        _new_file_diagnostics(app, result)


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
    _scene_state_diagnostics(app, scene, result)
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


def _border_sample_clips(region: Region) -> list[Region]:
    """Sample outer horizontal edges and three pairs of side cells, without padding."""
    width, height = region.width, region.height
    if width <= 0 or height <= 0:
        return []
    clips = [Region(0, y, min(width, MAX_CELLS), 1) for y in sorted({0, height - 1})]
    side_rows = sorted({1, height // 2, height - 2} - {0, height - 1})
    clips.extend(
        Region(x, y, 1, 1) for y in side_rows if 0 < y < height - 1 for x in sorted({0, width - 1})
    )
    return clips


def _border_sample(widget: Any, composed: Any, clip: Region) -> dict[str, Any]:
    """Map a native outer-widget clip to screen coordinates exactly once."""
    outer = widget.region
    x, y = outer.x + clip.x, outer.y + clip.y
    sample: dict[str, Any] = {
        "widget_local_clip": _region_record(clip),
        "screen_clip": _region_record(Region(x, y, clip.width, 1)),
        "styled_widget_row": None,
        "compositor_row": None,
        "missing": [],
    }
    try:
        strip = widget.render_lines(clip)[0]
        sample["styled_widget_row"] = strip_record(strip, x=x, y=y, width=clip.width)
    except (AttributeError, IndexError, RuntimeError, TypeError, ValueError) as error:
        sample["missing"].append("styled_widget_row:" + type(error).__name__)
    try:
        if x < 0 or not 0 <= y < len(composed):
            sample["missing"].append("outside_screen")
        else:
            crop = composed[y].crop(x, x + clip.width)
            sample["compositor_row"] = strip_record(crop, x=x, y=y, width=clip.width)
    except (AttributeError, IndexError, RuntimeError, TypeError, ValueError) as error:
        sample["missing"].append("compositor_row:" + type(error).__name__)
    for layer in ("styled_widget_row", "compositor_row"):
        row = sample[layer]
        if row is None or row["cells"] != clip.width or row["truncated"]:
            sample["missing"].append(layer + ":unavailable_or_clipped")
    sample["layers_equal"] = rendered_rows_equal(
        sample["styled_widget_row"], sample["compositor_row"]
    )
    return sample


def _unsampled_intervals(length: int, intervals: list[tuple[int, int]]) -> list[list[int]]:
    """Record half-open gaps without allocating a cell for every perimeter coordinate."""
    gaps = []
    end = 0
    for left, right in sorted(intervals):
        if left > end:
            gaps.append([end, left])
        end = max(end, right)
    if end < length:
        gaps.append([end, length])
    return gaps


def _border_coverage(region: Region, clips: list[Region]) -> dict[str, Any]:
    """Identify every unsampled edge coordinate, including partially sampled corners."""
    edges = {
        "top": (region.width, [(c.x, c.right) for c in clips if c.y == 0]),
        "bottom": (region.width, [(c.x, c.right) for c in clips if c.y == region.height - 1]),
        "left": (region.height, [(c.y, c.bottom) for c in clips if c.x == 0]),
        "right": (region.height, [(c.y, c.bottom) for c in clips if c.right == region.width]),
    }
    return {
        "unsampled_edges": {
            name: _unsampled_intervals(length, intervals)
            for name, (length, intervals) in edges.items()
        },
        "horizontal_cell_limit": region.width > MAX_CELLS,
        "sampled_clips": [_region_record(clip) for clip in clips],
        "scope": "outer perimeter only; interior and descendant output are not fully sampled",
    }


def _append_border_sample(entry: dict[str, Any], sample: dict[str, Any]) -> None:
    """Keep positioned styled/composed rows available to the existing SVG mapper."""
    entry["border_samples"].append(sample)
    for layer, rows in (
        ("styled_widget_row", "widget_rows"),
        ("compositor_row", "composed_rows"),
    ):
        if sample[layer] is not None:
            entry[rows].append(sample[layer])


def _collect_border_raw(widget: Any, entry: dict[str, Any], result: dict[str, Any]) -> None:
    """Retain the original raw content strip with its own coordinate system."""
    try:
        content = widget.content_region
        raw = widget.render_line(0)
        row = strip_record(raw, x=content.x, y=content.y, width=content.width)
        observation = ruler_strip_observation(raw, row, width=content.width)
        entry.update(raw_content_row=row, raw_content_observation=observation)
        if not observation["complete"] or row["truncated"]:
            result["omissions"].append(entry["name"] + ":raw_content_incomplete")
    except (AttributeError, IndexError, RuntimeError, TypeError, ValueError) as error:
        result["missing"].append(entry["name"] + ":raw_content:" + type(error).__name__)


def _collect_border_region(
    target: tuple[str, Any, int], composed: Any, result: dict[str, Any], row_budget: int
) -> int:
    """Share scene budgets while keeping raw content distinct from styled border output."""
    name, widget, requested_rows = target
    entry: dict[str, Any] = {
        "name": name,
        "region": _region_record(widget.region),
        "widget_rows": [],
        "composed_rows": [],
        "border_samples": [],
    }
    result["regions"].append(entry)
    try:
        entry["geometry"] = _selector_child_state(widget)
        if entry["geometry"]["ancestors_truncated"]:
            result["omissions"].append(name + ":ancestor_limit")
        if not widget.display or not widget.visible or not widget.region.area:
            result["missing"].append(name + ":not_visible")
            return row_budget
        if row_budget:
            _collect_border_raw(widget, entry, result)
            row_budget -= 1
        clips = _border_sample_clips(widget.region)
        selected = clips[: min(row_budget, max(0, requested_rows - 1))]
        entry["coverage"] = _border_coverage(widget.region, selected)
        if len(selected) != len(clips):
            result["omissions"].append(name + ":row_limit")
        if any(entry["coverage"]["unsampled_edges"].values()):
            result["omissions"].append(name + ":unsampled_edges")
        if entry["coverage"]["horizontal_cell_limit"]:
            result["omissions"].append(name + ":cell_limit")
        for clip in selected:
            sample = _border_sample(widget, composed, clip)
            _append_border_sample(entry, sample)
            result["missing"].extend(name + ":" + issue for issue in sample["missing"])
        row_budget -= len(selected)
    except (AttributeError, IndexError, RuntimeError, TypeError, ValueError) as error:
        result["missing"].append(name + ":" + type(error).__name__)
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
        _scene_state_diagnostics(app, scene, result)
        if "source_selector" in result:
            _selector_visibility(result["source_selector"], [])
        return result
    targets.extend(selector_targets)
    if len(targets) > MAX_REGIONS:
        result["omissions"].append("region_limit")
        result["omissions"].extend(f"{name}:region_limit" for name, *_rest in targets[MAX_REGIONS:])
    row_budget = MAX_ROWS
    collector = _collect_border_region if scene in BORDER_SCENES else _collect_target_region
    for target in targets[:MAX_REGIONS]:
        row_budget = collector(target, composed, result, row_budget)
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
                        "text-decoration",
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
