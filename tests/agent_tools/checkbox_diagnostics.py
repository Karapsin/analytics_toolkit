from __future__ import annotations

import json
from itertools import product
from types import SimpleNamespace
from typing import Any

import pytest
from rich.segment import Segment
from rich.text import Text
from textual.geometry import Region
from textual.strip import Strip

from agent_tools import sql_explorer_render_diagnostics as diagnostics
from tests.agent_tools.render_diagnostics import _assert_form_scene


def _app(
    monkeypatch: pytest.MonkeyPatch, state: tuple[bool, bool, bool, bool], broken: str
) -> Any:
    checked, disabled, focused, advanced = state
    label = f"insert_data: {checked}"
    indicator = "[x]" if checked else "[ ]"
    complete = f"{indicator} {label}"
    region = Region(0, 0, 32, 1)
    styles = SimpleNamespace(**dict.fromkeys(diagnostics._SELECTOR_STYLES, "auto"))
    styles.opacity = styles.text_opacity = 1.0
    ancestor = SimpleNamespace(
        id="fields",
        region=region,
        content_region=region,
        scrollable_content_region=region,
        display=True,
        visible=True,
        styles=styles,
        parent=None,
    )
    widget = SimpleNamespace(
        id="create-insert_data",
        classes=set(),
        region=region,
        content_region=region,
        parent=ancestor,
        is_mounted=True,
        visible=True,
        display=True,
        disabled=disabled,
        has_focus=focused,
        value=checked,
        label=Text(label),
        styles=styles,
        _render_button=lambda: Text(indicator),
        render=lambda: Text(complete),
        render_lines=lambda _region: [
            Strip([Segment(indicator + " insert_data:" if broken == "widget" else complete)])
        ],
    )
    composed = indicator + " insert_data:" if broken == "composed" else complete
    app = SimpleNamespace(
        size=SimpleNamespace(width=208, height=47),
        screen=SimpleNamespace(
            query_one=lambda _query: SimpleNamespace(collapsed=not advanced),
            _compositor=SimpleNamespace(
                render_strips=lambda: [Strip([Segment(composed.ljust(32))])]
            ),
        ),
    )
    monkeypatch.setattr(diagnostics, "_targets", lambda *_args: [("insert_data", widget, 1)])
    return app


@pytest.mark.parametrize("state", list(product((False, True), repeat=4)))
@pytest.mark.parametrize("broken", ["", "widget", "composed"])
def test_checkbox_layers_preserve_intact_model_and_expose_text_loss(
    monkeypatch: pytest.MonkeyPatch, state: tuple[bool, bool, bool, bool], broken: str
) -> None:
    checked, disabled, focused, advanced = state
    app = _app(monkeypatch, state, broken)
    terminal = diagnostics.terminal_diagnostics(app, "create-table-advanced")
    snapshot = diagnostics.checkbox_failure_snapshot(
        app, "create-table-advanced", terminal=terminal
    )
    assert snapshot["complete"]
    assert snapshot["versions"]["python"]
    assert snapshot["versions"]["textual"]
    assert snapshot["versions"]["rich"]
    assert terminal["checkbox_context"]["advanced_collapsed"] is not advanced
    entry = snapshot["terminal"]["regions"][0]
    assert entry["state"]["value"] is checked
    assert entry["state"]["enabled"] is not disabled
    assert entry["state"]["focused"] is focused
    assert entry["state"]["label"] == f"insert_data: {checked}"
    assert entry["checkbox"]["direct_render"]["text"].endswith(str(checked))
    assert not entry["checkbox"]["direct_render"]["truncated"]
    assert entry["checkbox"]["geometry"]["ancestors"][0]["clip"]["width"] == 32
    for layer, identity in (("widget_rows", "widget"), ("composed_rows", "composed")):
        assert entry[layer][0]["text"].rstrip().endswith(str(checked)) is (broken != identity)


@pytest.mark.parametrize("layer", ["widget_rows", "composed_rows"])
def test_strict_form_failure_keeps_original_rows_without_recollecting(
    monkeypatch: pytest.MonkeyPatch, layer: str
) -> None:
    app = _app(
        monkeypatch,
        (False, True, False, True),
        "widget" if layer == "widget_rows" else "composed",
    )
    terminal = diagnostics.terminal_diagnostics(app, "create-table-advanced")
    monkeypatch.setattr(
        diagnostics, "terminal_diagnostics", lambda *_args: pytest.fail("must not recollect")
    )
    with pytest.raises(AssertionError) as failure:
        _assert_form_scene(terminal, app, "create-table-advanced")
    snapshot, _end = json.JSONDecoder().raw_decode(str(failure.value))
    assert snapshot["terminal"]["regions"][0][layer][0]["text"].rstrip() == "[ ] insert_data:"
    assert snapshot["terminal"]["regions"][0]["checkbox"]["direct_render"]["text"].endswith("False")
    assert snapshot["scene_id"] == "create-table-advanced"


def test_checkbox_snapshot_bounds_report_omissions_without_mutating_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch, (False, False, False, False), "")
    terminal = diagnostics.terminal_diagnostics(app, "create-table-advanced")
    before = json.dumps(terminal, sort_keys=True)
    monkeypatch.setattr(diagnostics, "MAX_BYTES", 1024)
    snapshot = diagnostics.checkbox_failure_snapshot(
        app, "create-table-advanced", terminal=terminal
    )
    assert not snapshot["complete"]
    assert "byte_limit" in snapshot["omissions"]
    assert len(json.dumps(snapshot, indent=4, sort_keys=True).encode()) <= 1024
    assert json.dumps(terminal, sort_keys=True) == before


@pytest.mark.parametrize(
    "observation", ["long_label", "long_render", "ancestors", "unsupported", "error"]
)
def test_checkbox_missing_and_truncated_observations_are_explicit(
    monkeypatch: pytest.MonkeyPatch, observation: str
) -> None:
    app = _app(monkeypatch, (False, False, False, False), "")
    widget = diagnostics._targets(app, "create-table-advanced")[0][1]
    if observation == "long_label":
        widget.label = Text("x" * (diagnostics.MAX_CELLS * diagnostics.MAX_ROWS + 1))
    elif observation == "long_render":
        widget.render = lambda: Text("x" * (diagnostics.MAX_CELLS * diagnostics.MAX_ROWS + 1))
    elif observation == "ancestors":
        widget.parent.parent = widget.parent
    elif observation == "unsupported":
        widget.render = object
    else:

        def fail() -> Text:
            message = "direct render failed"
            raise RuntimeError(message)

        widget.render = fail
    terminal = diagnostics.terminal_diagnostics(app, "create-table-advanced")
    snapshot = diagnostics.checkbox_failure_snapshot(
        app, "create-table-advanced", terminal=terminal
    )
    assert not snapshot["complete"]
    reasons = {
        "long_label": "insert_data:label_limit",
        "long_render": "insert_data:direct_render_limit",
        "ancestors": "insert_data:ancestor_limit",
        "unsupported": "insert_data:unsupported_direct_render",
        "error": "insert_data:checkbox:RuntimeError",
    }
    assert reasons[observation] in snapshot["missing"] + snapshot["omissions"]
    assert snapshot["terminal"]["regions"][0]["widget_rows"]


def test_missing_advanced_context_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(monkeypatch, (False, False, False, False), "")

    def missing(_query: str) -> Any:
        message = "missing advanced panel"
        raise diagnostics.NoMatches(message)

    app.screen.query_one = missing
    terminal = diagnostics.terminal_diagnostics(app, "create-table-advanced")
    assert "checkbox_context:NoMatches" in terminal["missing"]
