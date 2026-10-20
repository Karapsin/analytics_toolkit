from __future__ import annotations

import base64
from copy import deepcopy
from io import BytesIO
from typing import Any
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from agent_tools import sql_explorer_svg_evidence as evidence

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="5" viewBox="0 0 16 10">'
    '<g transform="translate(0,0.2)">'
    '<line data-terminal-underline="1" x1="2" x2="10" y1="4" y2="4" '
    'stroke="#eeeeee" stroke-width="2"/></g></svg>'
)


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(evidence, "RASTER_VIEWPORT", (8, 5))
    calls = []

    def render(svg: str) -> bytes:
        calls.append(svg)
        root = ET.fromstring(svg)
        size = (int(root.attrib["width"]), int(root.attrib["height"]))
        with Image.new("RGB", size, "#123456") as image, BytesIO() as output:
            image.save(output, format="PNG")
            return output.getvalue()

    monkeypatch.setattr(evidence, "_render_svg", render)
    retained: dict[str, bytes] = {}
    png, metadata = evidence.rasterize_framed_svg(
        SVG, retained_pngs=retained, normalize_underlines=False
    )
    return {"png": png, "metadata": metadata, "retained": retained, "calls": calls}


def _compare(state: dict[str, Any]) -> dict[str, Any]:
    return evidence.phase_rasterization_comparisons(
        SVG,
        state["png"],
        rasterization=state["metadata"],
        intermediate_png=state["retained"].get("intermediate"),
    )


def test_phase_pair_keeps_actual_control_stages_and_all_diagnostic_bytes(
    stages: dict[str, Any],
) -> None:
    before = deepcopy((stages["png"], stages["metadata"], stages["retained"]))
    result = _compare(stages)
    assert result["complete"], result["missing"]
    assert len(result["variants"]) == 6
    assert len(stages["calls"]) == 4
    for name, record in result["variants"].items():
        source = record["svg"]["content"].encode("utf-8")
        png = base64.b64decode(record["png"]["content"], validate=True)
        assert record["svg"]["sha256"] == evidence.digest(source)
        assert record["svg"]["byte_length"] == len(source)
        assert record["png"]["sha256"] == evidence.digest(png)
        assert record["png"]["byte_length"] == len(png)
        assert record["png"]["dimensions"] == (
            [16, 10] if name.startswith("intermediate") else [8, 5]
        )
        if name in {"intermediate_control", "reduced_control"}:
            stage = "intermediate" if name.startswith("intermediate") else "final"
            assert png == stages["retained"][stage]
            assert record["png"]["reused_ordinary_png"]
        elif name.endswith("_aligned"):
            assert not record["png"]["reused_ordinary_png"]
    assert before == (stages["png"], stages["metadata"], stages["retained"])


def test_missing_intermediate_is_incomplete_without_recollection(stages: dict[str, Any]) -> None:
    stages["retained"].pop("intermediate")
    result = _compare(stages)
    assert not result["complete"]
    assert result["variants"]["intermediate_control"]["reason"] == "actual_intermediate_unavailable"
    assert result["variants"]["reduced_control"]["status"] == "ok"
    assert len(stages["calls"]) == 4


def test_shared_limits_count_duplicate_logical_bytes_across_sections(
    monkeypatch: pytest.MonkeyPatch, stages: dict[str, Any]
) -> None:
    result = _compare(stages)
    geometry = {
        "phase_comparisons": result,
        "rasterization_comparisons": deepcopy(result),
        "ordinary_raster_stages": evidence.retained_stage_evidence(stages["retained"]),
    }
    evidence.bound_capture_comparisons(geometry)
    totals = result["shared_capture_budget"]["logical_bytes"]
    saved = deepcopy(geometry)
    monkeypatch.setattr(evidence, "MAX_SVG_BYTES", totals["UTF-8"])
    monkeypatch.setattr(evidence, "MAX_COMPARISON_PNG_BYTES", totals["base64"])
    evidence.bound_capture_comparisons(geometry)
    assert all(section["complete"] for section in geometry.values())
    monkeypatch.setattr(evidence, "MAX_SVG_BYTES", totals["UTF-8"] - 1)
    evidence.bound_capture_comparisons(geometry)
    assert all(not section["complete"] for section in geometry.values())
    assert all(
        "content" not in record["svg"]
        for section in (geometry["phase_comparisons"], geometry["rasterization_comparisons"])
        for record in section["variants"].values()
    )
    assert saved["phase_comparisons"]["variants"]["direct_control"]["svg"]["content"] == SVG
    assert geometry["ordinary_raster_stages"]["stages"]["intermediate"]["content"]


def test_encoded_geometry_file_limit_preserves_hashes_and_explicit_failure(
    monkeypatch: pytest.MonkeyPatch, stages: dict[str, Any]
) -> None:
    result = _compare(stages)
    geometry = {"phase_comparisons": result}
    before = deepcopy(result["variants"]["direct_control"]["svg"])
    monkeypatch.setattr(evidence, "MAX_CAPTURE_FILE_BYTES", 128)
    evidence.bound_capture_comparisons(geometry)
    assert not geometry["capture_transport"]["complete"]
    assert geometry["capture_transport"]["per_file_byte_limit"] == 128
    assert not result["complete"]
    assert "capture_geometry_file_limit" in result["missing"]
    source = result["variants"]["direct_control"]["svg"]
    assert source["sha256"] == before["sha256"]
    assert source["byte_length"] == before["byte_length"]
    assert "content" not in source


def test_phase_transform_failure_retains_unmodified_control(stages: dict[str, Any]) -> None:
    stages["metadata"]["normalization"] = {
        "normalized_framed_svg": SVG.replace("translate(0,0.2)", "rotate(15)")
    }
    result = _compare(stages)
    assert not result["complete"]
    for name in ("direct_aligned", "intermediate_aligned", "reduced_aligned"):
        assert result["variants"][name]["status"] == "failed"
    for name in ("direct_control", "intermediate_control", "reduced_control"):
        assert result["variants"][name]["status"] == "ok"
