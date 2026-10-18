"""Harness-only, bounded observations of synchronous ruler lifecycle callbacks."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import wraps
from typing import Any

from textual.app import ScreenStackError

MAX_EVENTS = 64
MAX_TRACE_BYTES = 8192
_CALLBACKS = (
    "_activate_tab",
    "_finish_added_workspace",
    "_restore_active_focus",
    "_switch_visual_database",
    "_command_database",
)
_SCHEDULERS = ("call_after_refresh", "call_later", "set_timer")


def _identity(widget: Any) -> dict[str, Any] | None:
    if widget is None:
        return None
    return {
        "class": type(widget).__name__,
        "id": getattr(widget, "id", None),
        "identity": id(widget),
    }


def _focus_state(app: Any) -> dict[str, Any]:
    try:
        workspace = app.active_workspace
        return {
            "active_tab": workspace.tab_id,
            "focused": _identity(app.focused),
            "remembered_focus": _identity(workspace.last_focused),
        }
    except (AttributeError, LookupError, RuntimeError, ScreenStackError) as error:
        return {"unavailable": type(error).__name__}


class RulerLifecycleTrace:
    """Forward original calls in place; diagnostics never schedule extra work."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.events: list[dict[str, Any]] = []
        self.omissions: list[str] = []
        self._sequence = 0
        self._originals: list[tuple[str, bool, Any]] = []
        self._installed = False

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(
            {"events": self.events, "complete": not self.omissions, "omissions": self.omissions}
        )

    def record(self, phase: str, callback: str, **details: Any) -> None:
        """Contain probe failures without replacing a callback's result or error."""
        try:
            self._record_event(phase, callback, **details)
        except (AttributeError, LookupError, RuntimeError, TypeError, ValueError) as error:
            if not self.omissions:
                self.omissions.append(f"trace_collection_{type(error).__name__}")

    def _record_event(self, phase: str, callback: str, **details: Any) -> None:
        """Keep the first events, reserving room for explicit overflow metadata."""
        self._sequence += 1
        if self.omissions:
            return
        event = {
            "sequence": self._sequence,
            "phase": phase,
            "callback": callback,
            "state": _focus_state(self.app),
            **details,
        }
        candidate = {
            "events": [*self.events, event],
            "complete": False,
            "omissions": ["trace_event_limit", "trace_byte_limit"],
        }
        if len(self.events) >= MAX_EVENTS:
            self.omissions.append("trace_event_limit")
        elif len(json.dumps(candidate, ensure_ascii=True).encode("utf-8")) > MAX_TRACE_BYTES:
            self.omissions.append("trace_byte_limit")
        else:
            self.events.append(event)

    def _callback(self, name: str, original: Any) -> Any:
        @wraps(original)
        def observed(*args: Any, **kwargs: Any) -> Any:
            if not self._installed:
                return original(*args, **kwargs)
            self.record("before", name)
            returned = False
            try:
                result = original(*args, **kwargs)
                returned = True
                return result
            finally:
                self.record("after", name, outcome="returned" if returned else "raised")

        return observed

    def _scheduler(self, name: str, original: Any) -> Any:
        @wraps(original)
        def observed(*args: Any, **kwargs: Any) -> Any:
            # set_timer's delay precedes its callback; preserve both verbatim.
            index = 1 if name == "set_timer" else 0
            callback = args[index] if len(args) > index else kwargs.get("callback")
            callback_name = getattr(callback, "__name__", None)
            if self._installed and callback_name in _CALLBACKS:
                self.record("schedule_requested", callback_name, scheduler=name)
            return original(*args, **kwargs)

        return observed

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        for name in (*_CALLBACKS, *_SCHEDULERS):
            self._install_one(name)

    def _install_one(self, name: str) -> None:
        try:
            original = getattr(self.app, name)
        except AttributeError:
            self.record("unavailable", name)
            return
        owned = name in vars(self.app)
        self._originals.append((name, owned, original))
        wrapper = self._scheduler if name in _SCHEDULERS else self._callback
        setattr(self.app, name, wrapper(name, original))

    def close(self) -> None:
        """Restore instance overrides and class descriptors exactly as installed."""
        for name, owned, original in reversed(self._originals):
            if owned:
                setattr(self.app, name, original)
            else:
                delattr(self.app, name)
        self._originals.clear()
        self._installed = False
