from __future__ import annotations

import asyncio
import json
from importlib.metadata import version
from typing import TYPE_CHECKING, Any

import pytest
from rich.segment import Segment
from rich.style import Style
from textual.app import App
from textual.css.query import NoMatches
from textual.strip import Strip
from textual.widgets import Select

from agent_tools import sql_explorer_render_diagnostics as diagnostics
from agent_tools import sql_explorer_visual, sql_explorer_visual_scene
from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

    from textual.app import ComposeResult


def _scene(scene: str, tmp_path: Path) -> Any:
    return sql_explorer_visual_scene.create_scene(
        scene, tmp_path / "geometry.json", REPO_ROOT / sql_explorer_visual.MANIFEST
    )


@pytest.mark.parametrize("scene", sorted(diagnostics.SELECTOR_SCENES))
def test_repaired_selector_evidence_is_complete_visible_and_ready(
    scene: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def exercise() -> None:
        app = _scene(scene, tmp_path)
        async with app.run_test(size=(208, 47)) as pilot:
            await pilot.pause()
            for _attempt in range(20):
                if app.creation_scene_ready:
                    break
                await pilot.pause()
            assert app.creation_scene_ready
            terminal = diagnostics.terminal_diagnostics(app, scene)
            snapshot = terminal["source_selector"]
            expected = "from_sql" if scene == "create-table-sql" else "table_schema"
            assert snapshot["selected_value"] == expected
            assert snapshot["allow_blank"] is False
            assert snapshot["active_arrow"] == "down-arrow"
            assert not snapshot["collection_missing"]
            assert set(snapshot["children"]) == {
                "source-selector",
                "source-current",
                "source-label",
                "source-arrow",
                "source-inactive-arrow",
            }
            assert snapshot["children"]["source-label"]["renderable_text"] == expected
            assert not snapshot["children"]["source-inactive-arrow"]["display"]
            assert not any("inactive-arrow" in item for item in snapshot["findings"])
            assert snapshot["visibility_ok"] is True
            assert snapshot["findings"] == []
            for entry in terminal["regions"]:
                if entry["name"].startswith("source-") and entry["region"]["height"] > 0:
                    assert len(entry["widget_rows"]) == entry["region"]["height"]
                    assert len(entry["composed_rows"]) == entry["region"]["height"]
            assert sum(len(item["composed_rows"]) for item in terminal["regions"]) <= 24
            observation = app._selector_readiness_snapshot()
            assert observation["selector"] == snapshot
            assert all(diagnostics.selector_readiness_assertions(observation).values())
            assert "selector_readiness" not in app._scene_readiness_details()["conditions"]
            # Both existing GitHub Textual environments emit their actual observations.
            logged = diagnostics.bounded_payload(
                {
                    "textual": version("textual"),
                    "scene": scene,
                    "complete": not terminal["missing"] and not terminal["omissions"],
                    "missing": terminal["missing"][:],
                    "omissions": terminal["omissions"][:],
                    "terminal": {**terminal, "regions": terminal["regions"][:]},
                    "svg": {"elements": [], "omitted_elements": 0},
                }
            )
            encoded = json.dumps(logged, indent=4, sort_keys=True)
            assert len(encoded.encode()) <= diagnostics.MAX_BYTES
            with capsys.disabled():
                print(encoded)  # noqa: T201 - bounded GitHub child evidence.

    asyncio.run(exercise())


def test_selector_keyboard_switch_and_expanded_arrow_are_scoped(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = _scene("create-table-schema", tmp_path)
        async with app.run_test(size=(208, 47)) as pilot:
            await pilot.pause()
            selector = app.screen.query_one("#create-table-source", Select)
            # Another Select has identical internal child IDs; it must never be collected.
            other = Select([("unrelated", "unrelated")], allow_blank=False, id="other-select")
            await app.screen.mount(other)
            selector.focus()
            await pilot.press("enter")
            await pilot.pause()
            expanded = diagnostics.terminal_diagnostics(app, "create-table-schema")[
                "source_selector"
            ]
            assert expanded["expanded"] is True
            assert expanded["active_arrow"] == "up-arrow"
            assert expanded["children"]["source-arrow"]["renderable_text"] == "▲"
            assert expanded["children"]["source-label"]["renderable_text"] == "table_schema"
            await pilot.press("down", "enter")
            await pilot.pause()
            switched = diagnostics.terminal_diagnostics(app, "create-table-sql")["source_selector"]
            assert switched["selected_value"] == "from_sql"
            assert switched["children"]["source-label"]["renderable_text"] == "from_sql"
            assert switched["active_arrow"] == "down-arrow"

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["missing", "hidden", "zero-height", "clipped", "concealed"])
def test_child_failure_is_reported_when_outer_selector_remains_present(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        app = _scene("create-table-schema", tmp_path)
        async with app.run_test(size=(208, 47)) as pilot:
            await pilot.pause()
            selector = app.screen.query_one("#create-table-source", Select)
            label = selector.query_one("SelectCurrent > Static#label")
            if failure == "missing":
                lookup = selector.query_one

                def query(query: Any, *args: Any) -> Any:
                    if query == "SelectCurrent > Static#label":
                        message = "injected missing label"
                        raise NoMatches(message)
                    return lookup(query, *args)

                monkeypatch.setattr(selector, "query_one", query)
            elif failure == "hidden":
                label.display = False
            elif failure == "zero-height":
                label.styles.height = 0
            elif failure == "clipped":
                label.styles.offset = (500, 0)
            else:
                # Inject concealment at the cache boundary, preserving real DOM/layout.
                def concealed(region: Any) -> list[Strip]:
                    return [
                        Strip([Segment("table_schema", Style(conceal=True))])
                        for _row in range(region.height)
                    ]

                monkeypatch.setattr(label, "render_lines", concealed)
            await pilot.pause()
            terminal = diagnostics.terminal_diagnostics(app, "create-table-schema")
            snapshot = terminal["source_selector"]
            assert snapshot["children"]["source-selector"]["mounted"]
            assert snapshot["children"]["source-selector"]["region"]["height"] > 0
            assert snapshot["visibility_ok"] is False
            expected = {
                "missing": "source-label:missing_child",
                "hidden": "source-label:hidden_or_unmounted",
                "zero-height": "source-label:empty_content_region",
                "clipped": "source-label:ancestor_clipping",
                "concealed": "source-label:widget_rows:concealed_output",
            }[failure]
            assert expected in snapshot["findings"]
            if failure == "missing":
                assert expected in snapshot["collection_missing"]
            else:
                assert not snapshot["collection_missing"]

    asyncio.run(exercise())


def test_complete_child_content_rows_are_collected_beyond_outer_borders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SelectorApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Select(
                [("table_schema", "table_schema"), ("from_sql", "from_sql")],
                allow_blank=False,
                id="create-table-source",
            )

    monkeypatch.setattr(diagnostics, "_targets", lambda *_args: [])

    async def exercise() -> None:
        app = SelectorApp()
        async with app.run_test(size=(208, 47)) as pilot:
            await pilot.pause()
            result = diagnostics.terminal_diagnostics(app, "create-table-schema")
            assert not result["missing"]
            assert not result["omissions"]
            snapshot = result["source_selector"]
            assert snapshot["visibility_ok"] is True
            regions = {entry["name"]: entry for entry in result["regions"]}
            for name, expected in (("source-label", "table_schema"), ("source-arrow", "▼")):
                for layer in ("widget_rows", "composed_rows"):
                    rows = regions[name][layer]
                    assert len(rows) == regions[name]["region"]["height"]
                    assert expected in "\n".join(row["text"] for row in rows)
            outer = regions["source-selector"]
            assert len(outer["composed_rows"]) > 1
            assert any("table_schema" in row["text"] for row in outer["composed_rows"])

    asyncio.run(exercise())
