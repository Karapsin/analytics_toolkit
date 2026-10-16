"""Read-only, bounded failure observations for the type-menu regression."""

from __future__ import annotations

import asyncio
import json
import platform
from importlib.metadata import PackageNotFoundError, version
from typing import Any

MAX_SNAPSHOT_BYTES = 8 * 1024
_MAX_ITEMS = 32
_MAX_TEXT = 1024
_MISSING = object()


def _read(target: Any, name: str, path: str, unavailable: list[str]) -> Any:
    value = getattr(target, name, _MISSING)
    if value is _MISSING:
        unavailable.append(path + "." + name)
        return None
    return value


def _identity(widget: Any) -> dict[str, Any] | None:
    if widget is None:
        return None
    return {
        "object_id": id(widget),
        "class": type(widget).__name__,
        "id": getattr(widget, "id", None),
    }


def _scalar(value: Any, path: str, omissions: list[str]) -> Any:
    if isinstance(value, str) and len(value) > _MAX_TEXT:
        omissions.append(path + ":text_limit")
        return value[:_MAX_TEXT]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    omissions.append(path + ":unsupported_value")
    return {"class": type(value).__name__}


def _region(region: Any) -> dict[str, Any] | None:
    if region is None:
        return None
    return {name: getattr(region, name, None) for name in ("x", "y", "width", "height")}


def _widget(widget: Any, path: str, unavailable: list[str], omissions: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"identity": _identity(widget)}
    for name in (
        "has_focus",
        "is_mounted",
        "display",
        "visible",
        "scroll_x",
        "scroll_y",
        "scroll_target_x",
        "scroll_target_y",
        "_layout_required",
        "_repaint_required",
        "_scroll_required",
    ):
        result[name] = _scalar(_read(widget, name, path, unavailable), path + "." + name, omissions)
    for name in ("region", "content_region", "scrollable_content_region"):
        result[name] = _region(_read(widget, name, path, unavailable))
    queue = _read(widget, "_message_queue", path, unavailable)
    if isinstance(queue, asyncio.Queue):
        result["queued_messages"] = queue.qsize()
    else:
        result["queued_messages"] = None
        unavailable.append(path + ".queued_messages")
    return result


def _bounded_json(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=True)
    if len(encoded) <= MAX_SNAPSHOT_BYTES:
        return encoded
    # Keep the original observation's prefix, never recollect after an assertion.
    prefix = encoded
    while True:
        prefix = prefix[: len(prefix) // 2]
        bounded = json.dumps(
            {
                "complete": False,
                "omissions": ["snapshot_byte_limit"],
                "original_byte_length": len(encoded),
                "snapshot_prefix": prefix,
            },
            sort_keys=True,
            ensure_ascii=True,
        )
        if len(bounded) <= MAX_SNAPSHOT_BYTES:
            return bounded


def _package_version(package: str, unavailable: list[str]) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        unavailable.append("versions." + package)
        return "unavailable"


def menu_snapshot(app: Any, screen: Any, field: Any, menu: Any, fields: Any) -> str:
    """Observe without awaiting, dispatching, focusing, scrolling or refreshing."""
    unavailable: list[str] = []
    omissions: list[str] = []
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "focused_widget": _identity(app.focused),
        "type_input": _identity(screen.type_input),
        "type_input_is_field": screen.type_input is field,
        "type_index": screen.type_index,
        "accepted_type": _scalar(screen.accepted_type, "accepted_type", omissions),
        "field_value": _scalar(field.value, "field_value", omissions),
        "advanced_collapsed": screen.query_one("#create-table-advanced").collapsed,
    }
    matches = screen.type_matches
    snapshot["matches"] = [_scalar(match, "matches", omissions) for match in matches[:_MAX_ITEMS]]
    snapshot["match_count"] = len(matches)
    if len(matches) > _MAX_ITEMS:
        omissions.append("matches:item_limit")
    for path, widget in (
        ("app", app),
        ("screen", screen),
        ("field", field),
        ("menu", menu),
        ("viewport", fields),
    ):
        snapshot[path] = _widget(widget, path, unavailable, omissions)
    versions = {"python": platform.python_version()}
    for package in ("textual", "rich"):
        versions[package] = _package_version(package, unavailable)
    snapshot.update(
        versions=versions, unavailable=unavailable, omissions=omissions, complete=not omissions
    )
    return _bounded_json(snapshot)
