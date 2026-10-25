from __future__ import annotations

import base64
import json
from copy import deepcopy
from io import BytesIO
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from agent_tools import sql_explorer_svg_evidence as evidence
from agent_tools import sql_explorer_visual_capture as capture
from tests.agent_tools.svg_evidence import COMPARISON_SVG

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def raster_capture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    monkeypatch.setattr(evidence, "RASTER_VIEWPORT", (8, 5))
    font_path = tmp_path / "font.ttf"
    font_path.write_bytes(b"observed font bytes")
    probe = {
        "status": "ok",
        "stdout": {"text": f"DejaVu Sans Mono\t{font_path}\n", "truncated": False},
    }
    monkeypatch.setattr(evidence, "_comparison_font", lambda: {"available": True, "probe": probe})
    monkeypatch.setattr(evidence, "_font_context", lambda _svg: {"system_matches": []})
    calls = []

    def render(svg: str) -> bytes:
        calls.append(svg)
        root = ET.fromstring(svg)
        size = (16, 10) if root.get("width") == "16" else (8, 5)
        color = "#abcdef" if evidence.COMPARISON_FONT in svg else "#123456"
        with Image.new("RGB", size, color) as image, BytesIO() as output:
            image.save(output, format="PNG")
            return output.getvalue()

    monkeypatch.setattr(evidence, "_render_svg", render)
    framed = capture._frame_svg(COMPARISON_SVG)
    retained: dict[str, bytes] = {}
    png, metadata = evidence.rasterize_framed_svg(framed, retained_pngs=retained)
    return {
        "framed": framed,
        "png": png,
        "metadata": metadata,
        "retained": retained,
        "calls": calls,
        "probe": probe,
        "font_path": font_path,
    }


def _comparisons(state: dict[str, Any], *, intermediate: bool = True) -> dict[str, Any]:
    return evidence.caret_rasterization_comparisons(
        COMPARISON_SVG,
        state["framed"],
        state["png"],
        rasterization=state["metadata"],
        intermediate_png=state["retained"]["intermediate"] if intermediate else None,
    )


def test_six_images_preserve_actual_stages_inputs_hashes_and_font_only_control(
    raster_capture: dict[str, Any],
) -> None:
    state = raster_capture
    before = deepcopy((state["metadata"], state["retained"]))
    result = json.loads(json.dumps(_comparisons(state)))
    assert result["complete"]
    assert result["missing"] == []
    assert len(result["variants"]) == 6
    # One ordinary supersampling call, one font-control call, and two direct calls.
    assert state["calls"] == [
        evidence.supersampling_svg(state["framed"]),
        evidence.supersampling_svg(evidence._terminal_font_variant(state["framed"])),
        state["framed"],
        evidence._terminal_font_variant(state["framed"]),
    ]
    for name, record in result["variants"].items():
        assert record["status"] == "ok"
        data = base64.b64decode(record["png"]["content"], validate=True)
        assert record["png"]["byte_length"] == len(data)
        assert record["png"]["sha256"] == evidence.digest(data)
        assert record["png"]["dimensions"] == (
            [16, 10] if name.startswith("intermediate") else [8, 5]
        )
        source = record["svg"]["content"].encode("utf-8")
        assert record["svg"]["byte_length"] == len(source)
        assert record["svg"]["sha256"] == evidence.digest(source)
    for stage in ("intermediate", "reduced"):
        original = result["variants"][stage + "_original"]
        expected = state["retained"]["intermediate"] if stage == "intermediate" else state["png"]
        assert base64.b64decode(original["png"]["content"]) == expected
        assert original["png"]["reused_ordinary_png"]
        control = result["variants"][stage + "_dejavu"]
        assert not control["png"]["reused_ordinary_png"]
        assert original["processing"] == state["metadata"]["processing"]
        assert control["processing"]["reductions"] == 1
        assert control["processing"]["reduction_filter"] == "Image.Resampling.LANCZOS"
    assert before == (state["metadata"], state["retained"])
    files = result["font_files"]
    assert files["complete"]
    assert files["files"] == [
        {
            "path": str(state["font_path"]),
            "status": "ok",
            "byte_length": len(b"observed font bytes"),
            "sha256": evidence.digest(b"observed font bytes"),
        }
    ]
    assert "unobservable" in files["renderer_selection"]


@pytest.mark.parametrize("kind", ["svg", "png"])
@pytest.mark.parametrize("extra", [0, 1])
def test_all_six_stages_share_exact_budget_boundaries_without_mutating_inputs(
    monkeypatch: pytest.MonkeyPatch, raster_capture: dict[str, Any], kind: str, extra: int
) -> None:
    baseline = _comparisons(raster_capture)
    total = baseline[f"combined_{kind}_byte_length"]
    assert total == sum(record[kind]["byte_length"] for record in baseline["variants"].values())
    limit = "MAX_SVG_BYTES" if kind == "svg" else "MAX_COMPARISON_PNG_BYTES"
    monkeypatch.setattr(evidence, limit, total - extra)
    result = _comparisons(raster_capture)
    assert result["complete"] is (extra == 0)
    assert result[f"combined_{kind}_byte_length"] == total
    for record in result["variants"].values():
        assert ("content" in record[kind]) is (extra == 0)
        if kind == "svg" and extra and "png" in record:
            assert "content" not in record["png"]
    assert "content" in raster_capture["metadata"]["svg"]
    if extra:
        assert f"combined_{kind}_byte_limit" in result["missing"]


@pytest.mark.parametrize("failure", ["font", "renderer", "missing_intermediate", "font_file"])
def test_unavailable_or_failed_comparisons_remain_incomplete_with_ordinary_evidence(
    monkeypatch: pytest.MonkeyPatch, raster_capture: dict[str, Any], failure: str
) -> None:
    if failure == "font":
        monkeypatch.setattr(
            evidence,
            "_comparison_font",
            lambda: {"available": False, "probe": {"status": "unavailable"}},
        )
    elif failure == "renderer":

        def fail(_svg: str) -> bytes:
            message = "diagnostic renderer failed"
            raise RuntimeError(message)

        monkeypatch.setattr(evidence, "_render_svg", fail)
    elif failure == "font_file":
        raster_capture["font_path"].unlink()
    result = _comparisons(raster_capture, intermediate=failure != "missing_intermediate")
    assert not result["complete"]
    assert result["missing"]
    ordinary = result["variants"]["reduced_original"]
    assert base64.b64decode(ordinary["png"]["content"]) == raster_capture["png"]
    if failure == "missing_intermediate":
        assert (
            result["variants"]["intermediate_original"]["reason"]
            == "actual_intermediate_unavailable"
        )
    elif failure == "renderer":
        assert result["variants"]["intermediate_dejavu"]["status"] == "failed"
        assert result["variants"]["direct_original"]["status"] == "failed"
    elif failure == "font_file":
        assert result["font_files"]["files"][0]["reason"] == "FileNotFoundError"


@pytest.mark.parametrize("probe", ["unavailable", "truncated", "over_limit"])
def test_font_file_probes_are_bounded_and_honestly_incomplete(
    monkeypatch: pytest.MonkeyPatch, raster_capture: dict[str, Any], probe: str
) -> None:
    observed = deepcopy(raster_capture["probe"])
    if probe == "unavailable":
        observed["status"] = "unavailable"
    elif probe == "truncated":
        observed["stdout"]["truncated"] = True
    else:
        monkeypatch.setattr(evidence, "MAX_FONT_FILE_BYTES", 1)
    result = evidence._observed_font_files({"system_matches": []}, {"probe": observed})
    assert not result["complete"]
    if probe == "over_limit":
        assert result["files"][0]["reason"] == "font_file_byte_limit"
    else:
        assert result["files"] == []


def test_font_file_count_limit_retains_bounded_hashes_and_reports_omissions(
    tmp_path: Path,
) -> None:
    matches = []
    for index in range(evidence.MAX_FONT_REQUESTS + 1):
        path = tmp_path / f"font-{index}.ttf"
        path.write_bytes(b"font")
        matches.append(
            {
                "probe": {
                    "status": "ok",
                    "stdout": {"text": f"Family\t{path}\n", "truncated": False},
                }
            }
        )
    result = evidence._observed_font_files(
        {"system_matches": matches}, {"probe": matches[0]["probe"]}
    )
    assert not result["complete"]
    assert result["omitted_files"] == 1
    assert len(result["files"]) == evidence.MAX_FONT_REQUESTS
    assert all(record["sha256"] == evidence.digest(b"font") for record in result["files"])


def test_wrong_direct_dimensions_are_incomplete_without_replacing_observed_bytes(
    monkeypatch: pytest.MonkeyPatch, raster_capture: dict[str, Any]
) -> None:
    state = raster_capture
    render = evidence._render_svg
    wrong_size = state["retained"]["intermediate"]

    def observe_wrong_direct(svg: str) -> bytes:
        return wrong_size if svg == state["framed"] else render(svg)

    monkeypatch.setattr(evidence, "_render_svg", observe_wrong_direct)
    result = _comparisons(state)
    assert not result["complete"]
    record = result["variants"]["direct_original"]
    assert record["status"] == "failed"
    assert record["reason"] == "unexpected_stage_dimensions"
    assert record["png"]["dimensions"] == [16, 10]
    assert base64.b64decode(record["png"]["content"], validate=True) == wrong_size
    assert "direct_original:unexpected_stage_dimensions" in result["missing"]
    assert result["variants"]["reduced_original"]["status"] == "ok"


@pytest.mark.parametrize("stage", ["intermediate", "reduced"])
@pytest.mark.parametrize("damage", ["sha256", "dimensions"])
def test_retained_stages_must_match_the_ordinary_invocations_processing_evidence(
    raster_capture: dict[str, Any], stage: str, damage: str
) -> None:
    state = raster_capture
    key = "intermediate" if stage == "intermediate" else "final"
    state["metadata"]["processing"][key][damage] = "wrong hash" if damage == "sha256" else [99, 99]
    before = deepcopy((state["metadata"], state["retained"]))
    result = _comparisons(state)
    assert not result["complete"]
    record = result["variants"][stage + "_original"]
    assert record["status"] == "failed"
    assert record["reason"] == "ordinary_stage_metadata_mismatch"
    assert stage + "_original:ordinary_stage_metadata_mismatch" in result["missing"]
    original = state["retained"]["intermediate"] if stage == "intermediate" else state["png"]
    assert base64.b64decode(record["png"]["content"], validate=True) == original
    assert record["png"]["sha256"] == evidence.digest(original)
    assert before == (state["metadata"], state["retained"])
    assert result["variants"][stage + "_dejavu"]["status"] == "ok"


@pytest.mark.parametrize("stage", ["intermediate", "reduced"])
def test_invalid_retained_png_keeps_failure_bytes_and_counts_them_in_the_budget(
    monkeypatch: pytest.MonkeyPatch, raster_capture: dict[str, Any], stage: str
) -> None:
    state = raster_capture
    malformed = b"invalid retained PNG"
    if stage == "intermediate":
        state["retained"]["intermediate"] = malformed
    else:
        state["png"] = malformed
    before = deepcopy((state["metadata"], state["retained"]))
    result = _comparisons(state)
    assert not result["complete"]
    record = result["variants"][stage + "_original"]
    assert record["status"] == "failed"
    assert record["reason"] == "invalid_png_header"
    assert record["png"]["dimensions"] is None
    assert record["png"]["sha256"] == evidence.digest(malformed)
    assert record["png"]["byte_length"] == len(malformed)
    assert base64.b64decode(record["png"]["content"], validate=True) == malformed
    assert stage + "_original:invalid_png_header" in result["missing"]
    total = sum(record["png"]["byte_length"] for record in result["variants"].values())
    assert result["combined_png_byte_length"] == total
    assert result["variants"][stage + "_dejavu"]["status"] == "ok"
    assert before == (state["metadata"], state["retained"])

    monkeypatch.setattr(evidence, "MAX_COMPARISON_PNG_BYTES", total)
    boundary = _comparisons(state)
    assert "combined_png_byte_limit" not in boundary["missing"]
    retained = boundary["variants"][stage + "_original"]["png"]
    assert base64.b64decode(retained["content"], validate=True) == malformed
    monkeypatch.setattr(evidence, "MAX_COMPARISON_PNG_BYTES", total - 1)
    exceeded = _comparisons(state)
    assert not exceeded["complete"]
    assert exceeded["combined_png_byte_length"] == total
    assert "combined_png_byte_limit" in exceeded["missing"]
    assert all("content" not in entry["png"] for entry in exceeded["variants"].values())
    assert before == (state["metadata"], state["retained"])
