from __future__ import annotations

from copy import deepcopy
from io import StringIO
from xml.etree import ElementTree as ET

import pytest
from rich.console import Console
from rich.segment import Segment, Segments
from rich.style import Style

from agent_tools import sql_explorer_export_observer as observer
from agent_tools import sql_explorer_svg_borders as borders


def _fixture() -> tuple[str, dict, dict]:
    console = Console(file=StringIO(), width=20, record=True)
    console.print(
        Segments([Segment("│ X │\n│ Y │\nend\n", Style(color="#e2e2e2"))]),
        end="",
    )
    with observer.observe_export() as snapshot:
        svg = console.export_svg(unique_id="borders")
    samples = [
        {
            "screen_clip": {"x": x, "y": y, "width": 1, "height": 1},
            "layers_equal": True,
            "missing": [],
            "compositor_row": {"text": "│"},
        }
        for x in (0, 4)
        for y in (0, 1)
    ]
    terminal = {
        "regions": [
            {
                "name": "decorative-dialog",
                "region": {"x": 0, "y": -1, "width": 5, "height": 4},
                "decorative_border_roles": {"left": "solid", "right": "round"},
                "border_samples": samples,
            }
        ],
        "missing": [],
        "omissions": ["unsampled_edges"],
    }
    return svg, snapshot, terminal


def test_trial_preserves_glyphs_styles_clips_and_only_adds_verified_in_cell_lines() -> None:
    svg, snapshot, terminal = _fixture()
    before = deepcopy((snapshot, terminal))
    trial, records = borders.border_continuity_trial(svg, snapshot, terminal, raw_svg=svg)
    assert len(records) == 4
    root = ET.fromstring(trial)
    parents = {child: parent for parent in root.iter() for child in parent}
    for node in list(root.iter()):
        if node.get("data-terminal-border-trial") == "1":
            assert float(node.attrib["stroke-width"]) == 1
            assert float(node.attrib["y2"]) - float(node.attrib["y1"]) == pytest.approx(24.4)
            assert node.attrib["clip-path"].startswith("url(#borders-line-")
            parents[node].remove(node)
    assert ET.tostring(root) == ET.tostring(ET.fromstring(svg))
    assert (snapshot, terminal) == before
    assert terminal["omissions"] == ["unsampled_edges"]


@pytest.mark.parametrize("failure", ["literal", "overlay", "corner", "gap", "dashed"])
def test_trial_excludes_unproven_roles_overlays_corners_and_intentional_gaps(failure: str) -> None:
    svg, snapshot, terminal = _fixture()
    entry = terminal["regions"][0]
    if failure == "literal":
        entry["decorative_border_roles"] = {}
    elif failure == "overlay":
        for sample in entry["border_samples"]:
            if sample["screen_clip"]["y"] == 1:
                sample["layers_equal"] = False
    elif failure == "corner":
        entry["region"]["y"] = 0
    elif failure == "gap":
        entry["border_samples"] = [
            sample for sample in entry["border_samples"] if sample["screen_clip"]["y"] == 0
        ]
    else:
        entry["decorative_border_roles"] = {"left": "dashed", "right": "double"}
    assert borders.border_continuity_trial(svg, snapshot, terminal, raw_svg=svg) == (svg, [])
