from __future__ import annotations

import base64
import json
from io import BytesIO
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from agent_tools import sql_explorer_svg_evidence as evidence
from agent_tools import sql_explorer_visual_capture as capture
from tests.agent_tools.svg_evidence import COMPARISON_SVG, PNG, comparison_renderer

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["comparison_renderer"]


def test_outer_viewport_doubles_size_without_reserializing_the_target_frame() -> None:
    framed = capture._frame_svg(COMPARISON_SVG)
    svg = evidence.supersampling_svg(framed)
    assert svg.endswith(framed + "</svg>")
    root = ET.fromstring(svg)
    assert root.attrib == {
        "width": "2560",
        "height": "1600",
        "viewBox": "0 0 1280 800",
    }
    assert len(root) == 1
    assert ET.tostring(root[0]) == ET.tostring(ET.fromstring(framed))


@pytest.mark.parametrize("retain", [False, True])
def test_rasterization_reduces_once_and_records_exact_inputs_and_hashes(
    monkeypatch: pytest.MonkeyPatch, retain: bool
) -> None:
    monkeypatch.setattr(evidence, "RASTER_VIEWPORT", (8, 5))
    framed = capture._frame_svg(COMPARISON_SVG)
    with Image.new("RGB", (16, 10), "#abcdef") as image, BytesIO() as output:
        image.save(output, format="PNG")
        intermediate = output.getvalue()
    calls = []

    def render(svg: str) -> bytes:
        calls.append(svg)
        return intermediate

    reductions = []
    resize = Image.Image.resize

    def observe_resize(image: Any, size: Any, *, resample: Any) -> Any:
        reductions.append((image.size, size, resample))
        return resize(image, size, resample=resample)

    monkeypatch.setattr(evidence, "_render_svg", render)
    monkeypatch.setattr(Image.Image, "resize", observe_resize)
    retained: dict[str, bytes] = {}
    png, metadata = evidence.rasterize_framed_svg(framed, retained_pngs=retained if retain else None)
    assert retained == ({"intermediate": intermediate, "final": png} if retain else {})
    assert calls == [evidence.supersampling_svg(framed)]
    assert reductions == [((16, 10), (8, 5), Image.Resampling.LANCZOS)]
    assert evidence._png_dimensions(png) == [8, 5]
    with Image.open(BytesIO(png)) as image:
        assert image.getpixel((4, 2)) == (171, 205, 239)
    svg = metadata["svg"]
    assert svg["content"] == calls[0]
    assert svg["byte_length"] == len(calls[0].encode("utf-8"))
    assert svg["sha256"] == evidence.digest(calls[0].encode("utf-8"))
    assert metadata["renderer_arguments"] == {"input": "svg.content", "other_arguments": {}}
    processing = metadata["processing"]
    assert processing["scale_factor"] == 2
    assert processing["intermediate"] == {
        "dimensions": [16, 10],
        "sha256": evidence.digest(intermediate),
    }
    assert processing["final"] == {"dimensions": [8, 5], "sha256": evidence.digest(png)}
    assert processing["pillow_version"] == evidence._version("Pillow")
    assert processing["reduction_filter"] == "Image.Resampling.LANCZOS"
    assert processing["reductions"] == 1


@pytest.mark.parametrize("retain", [False, True])
@pytest.mark.parametrize("preframed", [False, True])
def test_png_writer_publishes_shared_rasterizer_output_and_returns_its_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, preframed: bool, retain: bool
) -> None:
    framed = capture._frame_svg(COMPARISON_SVG)
    metadata = {"processing": {"final": {"sha256": evidence.digest(PNG)}}}
    calls = []

    retained: dict[str, bytes] = {}

    def rasterize(
        svg: str, *, retained_pngs: dict[str, bytes] | None = None
    ) -> tuple[bytes, dict[str, Any]]:
        assert retained_pngs is (retained if retain else None)
        if retained_pngs is not None:
            retained_pngs.update(intermediate=b"actual intermediate", final=PNG)
        calls.append(svg)
        return PNG, metadata

    monkeypatch.setattr(capture, "rasterize_framed_svg", rasterize)
    destination = tmp_path / "screenshots" / "scene.png"
    result = capture._write_png(
        COMPARISON_SVG,
        destination,
        framed_svg=framed if preframed else None,
        retained_pngs=retained if retain else None,
    )
    assert result is metadata
    assert calls == [framed]
    assert destination.read_bytes() == PNG
    assert retained == ({"intermediate": b"actual intermediate", "final": PNG} if retain else {})


@pytest.mark.parametrize("failure", ["renderer", "header", "dimensions", "decode", "final"])
def test_rasterization_failures_propagate_without_target_size_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    monkeypatch.setattr(evidence, "RASTER_VIEWPORT", (1, 1))
    with Image.new("RGB", (2, 2)) as image, BytesIO() as output:
        image.save(output, format="PNG")
        valid = output.getvalue()
    calls = []

    def render(svg: str) -> bytes:
        calls.append(svg)
        if failure == "renderer":
            message = "renderer rejected input"
            raise RuntimeError(message)
        return {"header": b"bad", "dimensions": PNG, "decode": valid[:24]}.get(failure, valid)

    if failure == "final":
        monkeypatch.setattr(
            Image.Image, "resize", lambda *_args, **_kwargs: Image.new("RGB", (3, 3))
        )
    monkeypatch.setattr(evidence, "_render_svg", render)
    destination = tmp_path / "scene.png"
    error = RuntimeError if failure == "renderer" else (ValueError, OSError)
    with pytest.raises(error):
        capture._write_png(COMPARISON_SVG, destination)
    assert len(calls) == 1
    assert not destination.exists()


@pytest.mark.parametrize("kind", ["svg", "png"])
@pytest.mark.parametrize("extra", [0, 1])
def test_processed_comparison_reuses_only_final_png_and_shares_both_budgets(
    monkeypatch: pytest.MonkeyPatch,
    comparison_renderer: list[str],
    kind: str,
    extra: int,
) -> None:
    framed = capture._frame_svg(COMPARISON_SVG)
    content = evidence.supersampling_svg(framed)
    metadata = {
        "svg": {
            "encoding": "UTF-8",
            "content": content,
            "byte_length": len(content.encode("utf-8")),
            "sha256": evidence.digest(content.encode("utf-8")),
        },
        "renderer": "resvg_py.svg_to_bytes",
        "renderer_arguments": {"input": "svg.content", "other_arguments": {}},
        "processing": {"final": {"dimensions": [1, 1], "sha256": evidence.digest(PNG)}},
    }
    baseline = evidence.rasterization_comparisons(
        COMPARISON_SVG, framed, PNG, rasterization=metadata
    )
    assert baseline["complete"]
    assert len(comparison_renderer) == 7
    assert comparison_renderer[2] == framed
    assert content not in comparison_renderer
    total = baseline[f"combined_{kind}_byte_length"]
    limit = "MAX_SVG_BYTES" if kind == "svg" else "MAX_COMPARISON_PNG_BYTES"
    monkeypatch.setattr(evidence, limit, total - extra)
    result = evidence.rasterization_comparisons(COMPARISON_SVG, framed, PNG, rasterization=metadata)
    result = json.loads(json.dumps(result))
    assert result["complete"] is (extra == 0)
    assert result[f"combined_{kind}_byte_length"] == total
    assert total == sum(record[kind]["byte_length"] for record in baseline["variants"].values())
    processed = result["variants"]["framed_supersampled"]
    assert processed["processing"] == metadata["processing"]
    assert processed["svg"]["sha256"] == metadata["svg"]["sha256"]
    if "png" in processed:
        assert processed["png"]["sha256"] == evidence.digest(PNG)
    for identity, record in result["variants"].items():
        assert ("content" in record[kind]) is (extra == 0)
        if "png" in record:
            assert record["png"]["reused_ordinary_png"] is (identity == "framed_supersampled")
    if not extra:
        assert base64.b64decode(processed["png"]["content"], validate=True) == PNG
    else:
        assert f"combined_{kind}_byte_limit" in result["missing"]
    assert "content" in metadata["svg"]
