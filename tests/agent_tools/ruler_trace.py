from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip

from agent_tools import sql_explorer_lifecycle_trace as tracing
from agent_tools import sql_explorer_render_diagnostics as diagnostics


class LifecycleApp:
    def __init__(self) -> None:
        self.editor = SimpleNamespace(
            id="editor",
            text="SELECT",
            selections=((0, 1), (0, 4)),
            preferences={1: 4},
            scroll=(2, 0),
        )
        self.command = SimpleNamespace(id="command")
        self.focused = self.editor
        self.active_workspace = SimpleNamespace(tab_id="1", last_focused=self.editor)
        self.queued: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
        self.calls: list[Any] = []
        self.result = object()

    def _activate_tab(self, tab_id: str) -> object:
        self.calls.append(("activate", tab_id))
        self.active_workspace.tab_id = tab_id
        self.call_after_refresh(self._restore_active_focus)
        return self.result

    def _restore_active_focus(self) -> None:
        self.calls.append("restore")
        self.focused = self.active_workspace.last_focused

    def _switch_visual_database(self) -> object:
        self.calls.append("switch")
        self._activate_tab("2")
        self.focused = self.command
        return self.result

    def _finish_added_workspace(self, tab_id: str) -> object:
        return self._activate_tab(tab_id)

    def _command_database(self, values: list[str]) -> object:
        self.calls.append(values)
        return self.result

    def call_after_refresh(self, callback: Any, *args: Any, **kwargs: Any) -> object:
        self.queued.append((callback, args, kwargs))
        return self.result

    def call_later(self, callback: Any, *args: Any, **kwargs: Any) -> object:
        return self.call_after_refresh(callback, *args, **kwargs)

    def set_timer(self, delay: float, callback: Any, *, name: str) -> object:
        self.calls.append(("timer", delay, name))
        self.queued.append((callback, (), {}))
        return self.result


def test_trace_preserves_deferred_order_arguments_results_and_first_focus_state() -> None:
    app = LifecycleApp()
    original_members = vars(app).copy()
    editor_state = deepcopy(vars(app.editor))
    trace = tracing.RulerLifecycleTrace(app)
    trace.install()
    trace.install()
    assert app.set_timer(0.3, app._switch_visual_database, name="switch") is app.result
    assert app.calls == [("timer", 0.3, "switch")]
    assert len(app.queued) == 1
    callback, args, kwargs = app.queued.pop(0)
    assert callback(*args, **kwargs) is app.result
    assert app.focused is app.command
    assert app.calls == [("timer", 0.3, "switch"), "switch", ("activate", "2")]
    assert len(app.queued) == 1
    snapshot = trace.snapshot()
    assert snapshot["complete"]
    assert [(event["phase"], event["callback"]) for event in snapshot["events"]] == [
        ("schedule_requested", "_switch_visual_database"),
        ("before", "_switch_visual_database"),
        ("before", "_activate_tab"),
        ("schedule_requested", "_restore_active_focus"),
        ("after", "_activate_tab"),
        ("after", "_switch_visual_database"),
    ]
    assert snapshot["events"][0]["scheduler"] == "set_timer"
    assert snapshot["events"][0]["state"]["active_tab"] == "1"
    assert snapshot["events"][-1]["state"]["active_tab"] == "2"
    assert snapshot["events"][-1]["state"]["focused"]["identity"] == id(app.command)
    assert snapshot["events"][-1]["state"]["remembered_focus"]["identity"] == id(app.editor)

    callback, args, kwargs = app.queued.pop(0)
    assert callback(*args, **kwargs) is None
    assert app.focused is app.editor
    assert app.calls.count("restore") == 1
    assert len(snapshot["events"]) == 6
    assert trace.snapshot()["events"][-1]["state"]["focused"]["identity"] == id(app.editor)
    assert [event["sequence"] for event in trace.events] == list(range(1, 9))
    assert not app.queued
    assert vars(app.editor) == editor_state
    trace.close()
    trace.close()
    assert set(vars(app)) == set(original_members)
    assert all(vars(app)[name] is value for name, value in original_members.items())
    closed = trace.snapshot()
    callback(*args, **kwargs)
    assert app.calls.count("restore") == 2
    assert trace.snapshot() == closed


def test_trace_forwards_owned_callback_kwargs_and_original_exception_identity() -> None:
    app = LifecycleApp()
    failure = RuntimeError("original callback failure")
    calls = []

    def fail(value: object, *, marker: object) -> None:
        calls.append((value, marker))
        raise failure

    app._command_database = fail
    trace = tracing.RulerLifecycleTrace(app)
    trace.install()
    value, marker = object(), object()
    with pytest.raises(RuntimeError, match="original callback failure") as caught:
        app._command_database(value, marker=marker)
    assert caught.value is failure
    assert calls == [(value, marker)]
    assert trace.events[-1]["outcome"] == "raised"
    trace.close()
    assert app._command_database is fail


def test_scheduler_keyword_callback_and_unobserved_callback_are_forwarded_verbatim() -> None:
    app = LifecycleApp()
    trace = tracing.RulerLifecycleTrace(app)
    trace.install()
    values = ["warehouse"]
    assert app.call_later(callback=app._command_database, values=values) is app.result
    callback, args, kwargs = app.queued.pop(0)
    assert args == ()
    assert kwargs["values"] is values
    assert callback(*args, **kwargs) is app.result
    assert app.calls == [values]
    before = deepcopy(trace.snapshot())

    def unobserved() -> None:
        pass

    assert app.call_after_refresh(unobserved) is app.result
    assert app.queued[-1] == (unobserved, (), {})
    assert trace.snapshot() == before
    trace.close()


@pytest.mark.parametrize("limit", ["events", "bytes"])
def test_trace_keeps_first_events_with_explicit_bounded_overflow(
    monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    app = LifecycleApp()
    trace = tracing.RulerLifecycleTrace(app)
    trace.record("before", "first")
    first = trace.snapshot()["events"][0]
    if limit == "events":
        monkeypatch.setattr(tracing, "MAX_EVENTS", 1)
    else:
        monkeypatch.setattr(tracing, "MAX_TRACE_BYTES", 1024)
    for _index in range(100):
        trace.record("before", "next")
    snapshot = trace.snapshot()
    assert not snapshot["complete"]
    assert snapshot["omissions"] == [f"trace_{'event' if limit == 'events' else 'byte'}_limit"]
    assert snapshot["events"][0] == first
    assert len(snapshot["events"]) <= tracing.MAX_EVENTS
    assert len(json.dumps(snapshot, ensure_ascii=True).encode("utf-8")) <= tracing.MAX_TRACE_BYTES


def test_unavailable_callbacks_and_focus_probes_are_explicit() -> None:
    app = SimpleNamespace()
    trace = tracing.RulerLifecycleTrace(app)
    trace.install()
    assert len(trace.events) == 8
    assert all(event["phase"] == "unavailable" for event in trace.events)
    assert all(event["state"] == {"unavailable": "AttributeError"} for event in trace.events)
    trace.close()
    assert vars(app) == {}


@pytest.mark.parametrize("width", [3, 4, 8])
def test_strip_observation_preserves_original_padding_combining_and_wide_styles(width: int) -> None:
    segments = [
        Segment("界e", Style(color="#abcdef")),
        Segment("\u0301", Style(italic=True)),
        Segment("   ", Style(bgcolor="#123456", underline=True)),
    ]
    strip = Strip(segments)
    row = diagnostics.strip_record(strip, x=2, y=3, width=width)
    original_row = deepcopy(row)
    original_segments = list(strip)
    snapshot = diagnostics.ruler_strip_observation(strip, row, width=width)
    assert snapshot["complete"]
    assert snapshot["original_lengths"] == {"characters": 6, "cells": 6}
    assert snapshot["result_lengths"] == {"characters": len(row["text"]), "cells": row["cells"]}
    assert snapshot["requested_crop_width"] == width
    assert snapshot["segments"] == [
        {"text": segment.text, "style": str(segment.style), "control": str(segment.control)}
        for segment in segments
    ]
    assert snapshot["segments"][-1]["text"] == "   "
    assert snapshot["segments"][1]["text"] == "\u0301"
    assert row == original_row
    assert list(strip) == original_segments
    assert strip.text == "界e\u0301   "


def test_strip_observation_limit_retains_lengths_and_marks_missing_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strip = Strip([Segment("1" + " " * 205)])
    row = diagnostics.strip_record(strip, x=0, y=0, width=205)
    monkeypatch.setattr(diagnostics, "MAX_LIFECYCLE_BYTES", 256)
    snapshot = diagnostics.ruler_strip_observation(strip, row, width=205)
    assert not snapshot["complete"]
    assert snapshot["omissions"] == ["strip_observation_byte_limit"]
    assert snapshot["original_lengths"] == {"characters": 206, "cells": 206}
    assert snapshot["result_lengths"] == {"characters": 205, "cells": 205}
    assert "segments" not in snapshot
    assert len(json.dumps(snapshot).encode("utf-8")) <= diagnostics.MAX_LIFECYCLE_BYTES


def test_collection_failure_does_not_replace_callback_result_or_exception() -> None:
    app = LifecycleApp()
    trace = tracing.RulerLifecycleTrace(app)
    trace.install()
    app.focused.id = object()  # An unserializable probe is not a callback failure.
    values = ["warehouse"]
    assert app._command_database(values) is app.result
    assert app.calls == [values]
    assert trace.snapshot() == {
        "events": [],
        "complete": False,
        "omissions": ["trace_collection_TypeError"],
    }
    trace.close()


def test_scheduler_failure_is_forwarded_without_executing_or_retrying_callback() -> None:
    app = LifecycleApp()
    failure = RuntimeError("scheduler closed")
    calls = []

    def reject(callback: Any, *args: Any, **kwargs: Any) -> None:
        calls.append((callback, args, kwargs))
        raise failure

    app.call_after_refresh = reject
    trace = tracing.RulerLifecycleTrace(app)
    trace.install()
    callback = app._restore_active_focus
    with pytest.raises(RuntimeError, match="scheduler closed") as caught:
        app.call_after_refresh(callback)
    assert caught.value is failure
    assert calls == [(callback, (), {})]
    assert app.calls == []
    assert [event["phase"] for event in trace.events] == ["schedule_requested"]
    trace.close()
    assert app.call_after_refresh is reject


def test_strip_observation_budget_includes_completeness_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strip = Strip([Segment("1" + " " * 100)])
    row = diagnostics.strip_record(strip, x=0, y=0, width=101)
    expected = diagnostics.ruler_strip_observation(strip, row, width=101)
    size = len(json.dumps(expected, ensure_ascii=True).encode("utf-8"))
    monkeypatch.setattr(diagnostics, "MAX_LIFECYCLE_BYTES", size)
    assert diagnostics.ruler_strip_observation(strip, row, width=101) == expected
    monkeypatch.setattr(diagnostics, "MAX_LIFECYCLE_BYTES", size - 1)
    incomplete = diagnostics.ruler_strip_observation(strip, row, width=101)
    assert not incomplete["complete"]
    assert incomplete["omissions"] == ["strip_observation_byte_limit"]
    assert len(json.dumps(incomplete, ensure_ascii=True).encode("utf-8")) <= size - 1


def test_unavailable_strip_observation_does_not_replace_original_assertion() -> None:
    assert diagnostics.ruler_strip_observation(object(), {}, width=208) == {
        "complete": False,
        "missing": ["strip_observation:AttributeError"],
        "omissions": [],
    }
