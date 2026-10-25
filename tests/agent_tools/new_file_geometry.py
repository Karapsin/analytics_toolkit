from __future__ import annotations

from types import SimpleNamespace

import pytest
from textual.geometry import Region

from agent_tools import sql_explorer_visual_scene as scenes


@pytest.mark.parametrize("damage", ["none", "upper_left", "horizontal", "vertical", "clipped"])
def test_new_file_geometry_rejects_misalignment_without_rewriting_observations(damage: str) -> None:
    viewport = Region(3, 5, 100, 40)
    dialog = Region(23, 19, 60, 12)
    horizontal, vertical = "center", "middle"
    if damage == "upper_left":
        dialog = Region(3, 5, 60, 12)
        horizontal, vertical = "left", "top"
    elif damage == "horizontal":
        dialog = Region(22, 19, 60, 12)
    elif damage == "vertical":
        dialog = Region(23, 18, 60, 12)
    elif damage == "clipped":
        dialog = Region(2, 19, 60, 12)
    widget = SimpleNamespace(region=dialog)
    queries = []

    def query(selector: str) -> SimpleNamespace:
        queries.append(selector)
        return widget

    screen = SimpleNamespace(
        content_region=viewport,
        styles=SimpleNamespace(align_horizontal=horizontal, align_vertical=vertical),
        query_one=query,
    )
    result = scenes._new_file_alignment_assertions(SimpleNamespace(screen=screen))
    assert all(result.values()) is (damage == "none")
    assert result["new_file_dialog_contained"] is (damage != "clipped")
    assert result["new_file_center_middle"] is (damage != "upper_left")
    if damage in {"horizontal", "upper_left", "clipped"}:
        assert not result["new_file_horizontally_centered"]
    if damage in {"vertical", "upper_left"}:
        assert not result["new_file_vertically_centered"]
    assert queries == ["#new-file-dialog"]
    assert widget.region == dialog
    assert screen.content_region == viewport
    assert screen.styles.align_horizontal == horizontal
    assert screen.styles.align_vertical == vertical
