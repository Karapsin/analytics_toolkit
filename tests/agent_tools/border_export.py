from __future__ import annotations

from types import SimpleNamespace

from textual.geometry import Region

from agent_tools import sql_explorer_render_diagnostics as diagnostics


def test_searching_border_band_uses_same_screen_rows_for_both_nested_edges() -> None:
    dialog = SimpleNamespace(id="connections-picker", region=Region(49, 6, 110, 35))
    options = SimpleNamespace(
        id="connections-options", region=Region(50, 10, 108, 22), parent=dialog
    )
    sampled = []
    for widget in (dialog, options):
        before = widget.region
        clips = diagnostics._searching_border_clips(widget)
        assert len(clips) <= 11
        expected_rows = {24, 25, 26}
        right_rows = {
            widget.region.y + clip.y
            for clip in clips
            if clip.x == widget.region.width - 1 and clip.width == 1
        }
        assert expected_rows <= right_rows
        assert clips[:3] == [
            Region(widget.region.width - 1, y - widget.region.y, 1, 1)
            for y in sorted(expected_rows)
        ]
        coverage = diagnostics._border_coverage(widget.region, clips)
        assert coverage["unsampled_edges"]["right"]
        assert not coverage["unsampled_edges"]["top"]
        assert not coverage["unsampled_edges"]["bottom"]
        assert before == widget.region
        sampled.append(right_rows)
    assert {24, 25, 26} <= sampled[0] & sampled[1]


def test_searching_border_band_handles_small_or_empty_viewports_without_inventing_cells() -> None:
    for region in (Region(0, 0, 0, 0), Region(0, 0, 1, 1), Region(2, 3, 5, 2)):
        widget = SimpleNamespace(id="connections-picker", region=region)
        clips = diagnostics._searching_border_clips(widget)
        assert len(clips) == len(set(clips))
        assert all(0 <= clip.x < region.width and 0 <= clip.y < region.height for clip in clips)
