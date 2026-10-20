from __future__ import annotations

from copy import deepcopy
from io import StringIO
from xml.etree import ElementTree as ET

import pytest
from agent_tools.sql_explorer_svg_evidence import rasterize_framed_svg
from agent_tools.sql_explorer_svg_phase import align_generated_underlines
from agent_tools.sql_explorer_svg_whitespace import restore_omitted_underlines
from agent_tools.sql_explorer_visual_capture import _frame_svg
from rich.console import Console
from rich.segment import Segment, Segments
from rich.style import Style

from agent_tools import sql_explorer_export_observer as observer
from tests.agent_tools.underline_rasterization import _raster, _visible_underline


def _export(
    spaces: str = " ", *, last_row: bool = False, style: Style | None = None
) -> tuple[str, dict]:
    console = Console(file=StringIO(), record=True, width=208, color_system="truecolor")
    console.print(
        Segments(
            [
                Segment("A", Style(color="#ff0000")),
                Segment(spaces, style or Style(color="#e2e2e2", underline=True)),
                Segment("B" + ("" if last_row else "\nsecond row\n")),
            ]
        ),
        end="",
    )
    with observer.observe_export() as snapshot:
        svg = console.export_svg(unique_id="spaces", clear=False)
    return svg, snapshot


@pytest.mark.parametrize("spaces", [" ", "    "])
@pytest.mark.parametrize("dim", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_real_rich_omission_is_restored_without_text_or_style_changes(
    spaces: str, dim: bool, reverse: bool
) -> None:
    svg, snapshot = _export(
        spaces,
        style=Style(
            color="#e2e2e2", bgcolor="#20252a", underline=True, dim=dim, reverse=reverse
        ),
    )
    before = deepcopy(snapshot)
    repaired, records = restore_omitted_underlines(svg, snapshot)
    assert len(records) == 1
    record = records[0]
    assert record["export_cell"] == [1, 0]
    assert record["cells"] == len(spaces)
    track = snapshot["invocations"][0]["recording"]["export_coordinates"]
    expected = next(segment for segment in track["segments"] if segment["text"] == spaces)
    assert record["color"] == expected["resolved"]["foreground"]
    original = ET.fromstring(svg)
    root = ET.fromstring(repaired)
    strokes = [node for node in root.iter() if node.get("data-terminal-whitespace")]
    assert len(strokes) == 1
    parents = {child: parent for parent in root.iter() for child in parent}
    parents[strokes[0]].remove(strokes[0])
    assert ET.tostring(root) == ET.tostring(original)
    assert snapshot == before
    again, duplicates = restore_omitted_underlines(repaired, snapshot, raw_svg=svg)
    assert again == repaired
    assert duplicates == []


def test_last_row_missing_clip_and_other_snapshot_are_rejected() -> None:
    svg, snapshot = _export(last_row=True)
    with pytest.raises(ValueError, match="ambiguous_or_absent_line_clip"):
        restore_omitted_underlines(svg, snapshot)
    svg, snapshot = _export()
    with pytest.raises(ValueError, match="snapshot_export_identity_mismatch"):
        restore_omitted_underlines(svg + " ", snapshot)
    snapshot["invocations"][0]["recording"]["export_coordinates"]["complete"] = False
    with pytest.raises(ValueError, match="incomplete_export_coordinates"):
        restore_omitted_underlines(svg, snapshot)


def test_control_filtering_and_crop_follow_rich_without_rewriting_raw_stream() -> None:
    console = Console(file=StringIO(), record=True, width=4)
    segments = (
        Segment("\x1b[?25l", control=((6,),)),
        Segment("界e\u0301", Style(color="red")),
        Segment("    ", Style(underline=True)),
        Segment("\nnext\n"),
    )
    # Exercise the recording boundary directly: controls must retain their bytes,
    # while the independent export track must use Rich's actual crop algorithm.
    with console._record_buffer_lock:
        console._record_buffer.extend(segments)
    with observer.observe_export() as snapshot:
        svg = console.export_svg(clear=False)
    recording = snapshot["invocations"][0]["recording"]
    assert tuple(console._record_buffer) == segments
    assert recording["segments"][0]["text"] == "\x1b[?25l"
    assert "control_position_unresolved" in recording["missing"]
    track = recording["export_coordinates"]
    assert track["complete"]
    expected = list(Segment.split_and_crop_lines(Segment.filter_control(segments), length=4))
    assert [item["text"] for item in track["segments"]] == [
        segment.text for line in expected for segment in line
    ]
    observer.map_export_spans(snapshot, svg)
    mappings = snapshot["invocations"][0]["export_underline_span_mapping"]
    assert mappings
    assert mappings[0]["export_part"]["x"] == 3
    assert mappings[0]["export_part"]["cells"] == 1


@pytest.mark.parametrize("spaces", [" ", "    "])
def test_actual_raster_space_mark_has_causal_removal_color_cell_and_clip_controls(spaces: str) -> None:
    svg, snapshot = _export(spaces)
    repaired, records = restore_omitted_underlines(_frame_svg(svg), snapshot, raw_svg=svg)
    aligned, phases = align_generated_underlines(repaired)
    a, d, e, f = phases[0]["axis_mapping"]
    attributes = records[0]["attributes"]
    region = (
        float(attributes["x1"]) * a + e,
        records[0]["baseline"] * d + f,
        (float(attributes["x2"]) - float(attributes["x1"])) * a,
    )
    root = ET.fromstring(aligned)
    stroke = next(node for node in root.iter() if node.get("data-terminal-whitespace"))
    removed = deepcopy(root)
    parents = {child: parent for parent in removed.iter() for child in parent}
    absent = next(node for node in removed.iter() if node.get("data-terminal-whitespace"))
    parents[absent].remove(absent)
    plain_svg = ET.tostring(removed, encoding="unicode")
    with _raster(plain_svg) as plain, _raster(aligned) as image:
        assert _visible_underline(image, plain, region)
        assert not _visible_underline(plain, plain, region)
        assert not _visible_underline(image, plain, (region[0] + 100, region[1], region[2]))
        stroke.set("stroke", "#0000ff")
        with _raster(ET.tostring(root, encoding="unicode")) as wrong_color:
            assert not _visible_underline(wrong_color, plain, region)
        stroke.set("stroke", attributes["stroke"])
        clip = next(
            node for node in root.iter() if node.get("id") == records[0]["line_clip"]
        )
        clip[0].set("width", "1")
        with _raster(ET.tostring(root, encoding="unicode")) as clipped:
            assert not _visible_underline(clipped, plain, region)
    png, metadata = rasterize_framed_svg(repaired)
    assert png
    assert metadata["processing"]["reductions"] == 1
