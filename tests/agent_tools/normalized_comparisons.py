from __future__ import annotations

import base64
from typing import Any

import pytest

from agent_tools import sql_explorer_svg_evidence as evidence
from agent_tools import sql_explorer_visual_capture as capture
from tests.agent_tools.caret_rasterization import raster_capture

__all__ = ["raster_capture"]


def _normalized_capture(state: dict[str, Any]) -> dict[str, Any]:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="5">'
        '<style>.terminal-probe-matrix {font-family:monospace;font-size:20px}</style>'
        '<g class="terminal-probe-matrix"><text x="1" y="2" textLength="3" '
        'text-decoration="underline" fill="#abcdef">é</text></g></svg>'
    )
    framed = capture._frame_svg(svg)
    retained: dict[str, bytes] = {}
    png, metadata = evidence.rasterize_framed_svg(framed, retained_pngs=retained)
    state.update(svg=svg, framed=framed, png=png, metadata=metadata, retained=retained)
    return state


def _comparisons(state: dict[str, Any]) -> dict[str, Any]:
    return evidence.caret_rasterization_comparisons(
        state["svg"],
        state["framed"],
        state["png"],
        rasterization=state["metadata"],
        intermediate_png=state["retained"]["intermediate"],
    )


def test_twelve_images_keep_unmodified_inputs_and_bind_only_real_ordinary_output(
    raster_capture: dict[str, Any],
) -> None:
    state = _normalized_capture(raster_capture)
    result = _comparisons(state)
    assert result["complete"]
    assert len(result["variants"]) == 12
    for identity, record in result["variants"].items():
        normalized = identity.endswith("_normalized")
        content = record["svg"]["content"]
        assert ('data-terminal-underline="1"' in content) is normalized
        assert record["representation"] == (
            "explicit underline geometry" if normalized else "unmodified export"
        )
        ordinary = identity in {"intermediate_original_normalized", "reduced_original_normalized"}
        assert record["png"]["reused_ordinary_png"] is ordinary
        if ordinary:
            expected = (
                state["retained"]["intermediate"] if identity.startswith("intermediate") else state["png"]
            )
            assert base64.b64decode(record["png"]["content"], validate=True) == expected
        if "normalization" in record:
            assert "normalized_framed_svg" not in record["normalization"]
    exported = evidence.export_input_evidence(
        state["svg"], state["framed"], state["png"], rasterization=state["metadata"]
    )
    inputs = exported["inputs"]
    assert inputs["raw_svg"]["content"] == state["svg"]
    assert inputs["framed_svg"]["content"] == state["framed"]
    assert "data-terminal-underline" not in inputs["supersampling_svg"]["content"]
    assert 'data-terminal-underline="1"' in inputs["normalized_framed_svg"]["content"]
    assert inputs["normalized_supersampling_svg"] == state["metadata"]["svg"]
    assert exported["rasterization"]["svg_input"] == "inputs.normalized_supersampling_svg"


@pytest.mark.parametrize("kind", ["svg", "png"])
def test_original_and_normalized_stages_share_unchanged_exact_limits(
    monkeypatch: pytest.MonkeyPatch, raster_capture: dict[str, Any], kind: str
) -> None:
    state = _normalized_capture(raster_capture)
    baseline = _comparisons(state)
    total = sum(record[kind]["byte_length"] for record in baseline["variants"].values())
    assert baseline[f"combined_{kind}_byte_length"] == total
    limit = "MAX_SVG_BYTES" if kind == "svg" else "MAX_COMPARISON_PNG_BYTES"
    monkeypatch.setattr(evidence, limit, total)
    assert _comparisons(state)["complete"]
    monkeypatch.setattr(evidence, limit, total - 1)
    incomplete = _comparisons(state)
    assert not incomplete["complete"]
    assert f"combined_{kind}_byte_limit" in incomplete["missing"]
    assert all("content" not in record[kind] for record in incomplete["variants"].values())


def test_missing_diagnostic_font_keeps_both_representations_explicitly_incomplete(
    monkeypatch: pytest.MonkeyPatch, raster_capture: dict[str, Any]
) -> None:
    state = _normalized_capture(raster_capture)
    monkeypatch.setattr(
        evidence,
        "_comparison_font",
        lambda: {"available": False, "probe": {"status": "unavailable"}},
    )
    result = _comparisons(state)
    assert not result["complete"]
    assert len(result["variants"]) == 12
    assert all(
        record["status"] == "omitted"
        for identity, record in result["variants"].items()
        if "_dejavu" in identity
    )
    assert result["variants"]["reduced_original_normalized"]["status"] == "ok"
