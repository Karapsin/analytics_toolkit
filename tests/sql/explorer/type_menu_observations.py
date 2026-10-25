from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from textual.geometry import Region

from tests.sql._support import type_menu_diagnostics as diagnostics


def _context() -> tuple[Any, Any, Any, Any, Any]:
    async def assemble() -> tuple[Any, Any, Any, Any, Any]:
        def widget(identity: str) -> Any:
            queue: asyncio.Queue[object] = asyncio.Queue()
            return SimpleNamespace(
                id=identity,
                has_focus=False,
                is_mounted=True,
                display=True,
                visible=True,
                value="B",
                scroll_x=0,
                scroll_y=2,
                scroll_target_x=0,
                scroll_target_y=2,
                region=Region(4, 5, 20, 3),
                content_region=Region(4, 5, 20, 3),
                scrollable_content_region=Region(4, 5, 20, 3),
                _message_queue=queue,
                _layout_required=True,
                _repaint_required=False,
                _scroll_required=False,
            )

        app, screen, field, menu, fields = (
            widget(name) for name in ("app", "screen", "f", "m", "v")
        )
        field.has_focus = True
        app.focused = field
        screen.type_input = field
        screen.type_matches = ("BIGINT", "BOOLEAN")
        screen.type_index = 1
        screen.accepted_type = None
        screen.query_one = lambda _selector: SimpleNamespace(collapsed=False)
        for observed in (app, screen, field, menu, fields):
            await observed._message_queue.put(object())
        return app, screen, field, menu, fields

    return asyncio.run(assemble())


def test_menu_snapshot_retains_focus_geometry_queues_and_layout_without_mutation() -> None:
    context = _context()
    app, screen, field, menu, fields = context
    before = [vars(widget).copy() for widget in context]
    snapshot = diagnostics.menu_snapshot(*context)
    record = json.loads(snapshot)
    assert record["complete"]
    assert not record["unavailable"]
    assert not record["omissions"]
    assert record["focused_widget"] == record["field"]["identity"]
    assert record["type_input"] == record["field"]["identity"]
    assert record["type_input_is_field"]
    assert record["field_value"] == "B"
    assert record["matches"] == ["BIGINT", "BOOLEAN"]
    assert record["type_index"] == 1
    assert record["accepted_type"] is None
    assert record["advanced_collapsed"] is False
    assert record["versions"]["python"]
    assert record["versions"]["textual"]
    assert record["versions"]["rich"]
    for key in ("app", "screen", "field", "menu", "viewport"):
        assert record[key]["queued_messages"] == 1
        assert record[key]["_layout_required"] is True
        assert record[key]["_repaint_required"] is False
        assert record[key]["region"] == {"x": 4, "y": 5, "width": 20, "height": 3}
        assert record[key]["content_region"] == record[key]["region"]
    assert [vars(widget) for widget in context] == before
    assert all(widget._message_queue.qsize() == 1 for widget in context)
    assert app.focused is screen.type_input is field
    assert menu.display
    assert fields.scroll_y == 2
    assert diagnostics.menu_snapshot(*context) == snapshot


def test_missing_runtime_probes_and_dependency_versions_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    for widget in context:
        del widget._message_queue
        del widget._layout_required

    def unavailable(_package: str) -> str:
        raise diagnostics.PackageNotFoundError

    monkeypatch.setattr(diagnostics, "version", unavailable)
    record = json.loads(diagnostics.menu_snapshot(*context))
    assert record["menu"]["queued_messages"] is None
    assert record["menu"]["_layout_required"] is None
    assert "menu.queued_messages" in record["unavailable"]
    assert "menu._layout_required" in record["unavailable"]
    assert record["versions"]["textual"] == "unavailable"
    assert "versions.rich" in record["unavailable"]


def test_snapshot_retains_different_focused_and_tracked_fields() -> None:
    context = _context()
    app, screen, field, menu, _fields = context
    app.focused = menu
    screen.type_input = None
    menu.display = False
    field.is_mounted = False
    record = json.loads(diagnostics.menu_snapshot(*context))
    assert record["focused_widget"] == record["menu"]["identity"]
    assert record["type_input"] is None
    assert not record["type_input_is_field"]
    assert record["menu"]["display"] is False
    assert record["field"]["is_mounted"] is False


def test_snapshot_bound_reports_omission_and_retains_original_failure_observation() -> None:
    context = _context()
    app, screen, field, menu, fields = context
    screen.type_matches = ("界" * 2000,) * 40
    record = diagnostics.menu_snapshot(*context)
    assert len(record.encode("utf-8")) <= diagnostics.MAX_SNAPSHOT_BYTES
    bounded = json.loads(record)
    assert bounded["complete"] is False
    assert bounded["omissions"] == ["snapshot_byte_limit"]
    assert bounded["original_byte_length"] > diagnostics.MAX_SNAPSHOT_BYTES
    assert bounded["snapshot_prefix"]
    # A later state change must not replace the original assertion attachment.
    screen.type_matches = ()
    menu.display = False
    with pytest.raises(AssertionError) as failure:
        assert menu.display, record
    assert str(failure.value).startswith(record)
    assert app.focused is field
    assert fields.scroll_y == 2


def test_text_and_match_limits_are_explicit_before_whole_snapshot_bound() -> None:
    context = _context()
    screen = context[1]
    screen.accepted_type = "x" * 2000
    screen.type_matches = ("B",) * 40
    record = json.loads(diagnostics.menu_snapshot(*context))
    assert not record["complete"]
    assert record["omissions"] == ["accepted_type:text_limit", "matches:item_limit"]
    assert len(record["accepted_type"]) == 1024
    assert record["match_count"] == 40
    assert len(record["matches"]) == 32
