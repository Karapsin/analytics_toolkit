"""Bounded, non-mutating raster reproductions; never a visual acceptance verdict."""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import math
import platform
import traceback
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_tools.sql_explorer_svg_underlines import normalize_terminal_underlines

from agent_tools import sql_explorer_svg_evidence as evidence
from tests._support.paths import REPO_ROOT
from tests.agent_tools._support import raster_transport
from tests.agent_tools._support.raster_transport import source_binding
from tests.agent_tools._support.svg_fixtures import decode_captured_svg

if TYPE_CHECKING:
    from PIL import Image

MAX_REPORT_BYTES = 8 * 1024 * 1024
_ERRORS = (OSError, ValueError, RuntimeError, TypeError, LookupError, AssertionError, ET.ParseError)
SYNTHETIC_CASES = (
    ("space", " ", 12.2),
    ("expanded-tab", "    ", 48.8),
    ("wide-glyph", "界", 24.4),
    ("combining-mark", "é", 12.2),
)


def _failure() -> dict[str, str]:
    return {"status": "failed", "traceback": traceback.format_exc()}


def _identity(data: bytes) -> dict[str, Any]:
    return {"byte_length": len(data), "sha256": evidence.digest(data)}


class _Budget:
    def __init__(self) -> None:
        self.used = {"svg": 0, "png": 0}
        self.missing: list[str] = []

    def retain(self, kind: str, data: bytes) -> dict[str, Any]:
        limit = evidence.MAX_SVG_BYTES if kind == "svg" else evidence.MAX_COMPARISON_PNG_BYTES
        self.used[kind] += len(data)
        record = _identity(data)
        if self.used[kind] > limit:
            self.missing.append(kind + ":combined_byte_limit")
            return {**record, "status": "omitted", "reason": "combined_byte_limit"}
        return {
            **record,
            "status": "ok",
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
        }


def _png(image: Image.Image) -> bytes:
    with BytesIO() as stream:
        image.save(stream, format="PNG")
        return stream.getvalue()


def _stages(svg: str, budget: _Budget, record: dict[str, Any]) -> Image.Image:
    """Separate reproduction of the unchanged strict test's actual raster sequence."""
    supersampling = evidence.supersampling_svg(svg)
    record.update(
        {
            "invocation": "separate diagnostic reproduction; not reused pytest invocation bytes",
            "svg": budget.retain("svg", svg.encode("utf-8")),
            "supersampling_svg": budget.retain("svg", supersampling.encode("utf-8")),
            "renderer": "resvg_py.svg_to_bytes",
            "renderer_arguments": {"svg_string": "exact retained SVG", "other_arguments": {}},
            "streams": "In-process renderer streams belong to the bound GitHub command logs",
            "processing": {
                "reductions": 1,
                "filter": "Image.Resampling.LANCZOS",
                "conversion": "RGB before reduction, matching _raster",
            },
        }
    )
    direct = evidence._render_svg(svg)
    record["direct"] = budget.retain("png", direct)
    record["direct"]["dimensions"] = evidence._png_dimensions(direct)
    if record["direct"]["dimensions"] != list(evidence.RASTER_VIEWPORT):
        message = "diagnostic_direct_dimensions"
        raise ValueError(message)
    intermediate = evidence._render_svg(supersampling)
    record["intermediate"] = budget.retain("png", intermediate)
    dimensions = evidence._png_dimensions(intermediate)
    record["intermediate"]["dimensions"] = dimensions
    expected = [value * evidence.SUPERSAMPLE_FACTOR for value in evidence.RASTER_VIEWPORT]
    if dimensions != expected:
        message = "diagnostic_intermediate_dimensions"
        raise ValueError(message)
    image_module = importlib.import_module("PIL.Image")
    with image_module.open(BytesIO(intermediate)) as image:
        reduced = image.convert("RGB").resize(
            evidence.RASTER_VIEWPORT, image_module.Resampling.LANCZOS
        )
    record["reduced"] = {**budget.retain("png", _png(reduced)), "dimensions": list(reduced.size)}
    return reduced


def pixel_observations(
    image: Image.Image, control: Image.Image, region: tuple[float, float, float], api: Any
) -> dict[str, Any]:
    """Observe exactly the original predicate's cells, including all three rows."""
    x, baseline, width = region
    left, right = math.ceil(x), math.floor(x + width)
    rows = []
    for y in range(math.ceil(baseline), math.ceil(baseline) + 3):
        pixels, runs = [], []
        run = 0
        for column in range(left, right):
            actual, plain = image.getpixel((column, y)), control.getpixel((column, y))
            delta = [a - b for a, b in zip(actual, plain)]
            qualifying = all(value >= api._MIN_CONTRAST for value in delta)
            run = run + 1 if qualifying else 0
            pixels.append(
                {
                    "x": column,
                    "actual_rgb": actual,
                    "plain_rgb": plain,
                    "channel_differences": delta,
                    "qualifying": qualifying,
                }
            )
            runs.append(run)
        rows.append({"y": y, "pixels": pixels, "qualifying_runs": runs})
    return {
        "region": region,
        "left": left,
        "right_exclusive": right,
        "required_run": math.ceil(width * api._MIN_WIDTH_FRACTION),
        "minimum_contrast": api._MIN_CONTRAST,
        "minimum_width_fraction": api._MIN_WIDTH_FRACTION,
        "predicate_result": api._visible_underline(image, control, region),
        "rows": rows,
    }


def _variants(
    svg: str, api: Any, *, synthetic: bool, text: str = "", width: float = 0
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    normalized, conversions = normalize_terminal_underlines(svg)
    variants = {"original": svg, "plain": api._without_underline(svg), "normalized": normalized}
    if synthetic:
        root = ET.fromstring(normalized)
        line = next(node for node in root.iter() if node.get("data-terminal-underline") == "1")
        line.set("stroke", "#0000ff")
        variants["wrong-color"] = ET.tostring(root, encoding="unicode")
        variants["clipped"], _ = normalize_terminal_underlines(
            api._whitespace_svg(text, width, clipped=True)
        )
    return variants, conversions


def _captured_case(scene: str, api: Any) -> dict[str, Any]:
    directory = REPO_ROOT / "tests/agent_tools/_support/fixtures"
    provenance = json.loads((directory / "caret-export-provenance.json").read_bytes())
    fixture = next(item for item in provenance["fixtures"] if item["scene"] == scene)
    data = (directory / fixture["path"]).read_bytes()
    svg = decode_captured_svg(
        data, sha256=fixture["sha256"], envelope_sha256=fixture["envelope_sha256"]
    )
    variants, conversions = _variants(svg, api, synthetic=False)
    return {
        "name": scene,
        "variants": variants,
        "normalization": conversions,
        "regions": api._target_regions(svg, fixture["screen_cells"]),
        "mapping_inputs": {
            "screen_cells": fixture["screen_cells"],
            "algorithm": "unchanged _target_regions",
            "viewport": [1280, 800],
        },
        "fixture": {
            "envelope": _identity(data),
            "decoded": _identity(svg.encode("utf-8")),
            "provenance": fixture,
        },
    }


def _synthetic_case(name: str, text: str, width: float, api: Any) -> dict[str, Any]:
    svg = api._whitespace_svg(text, width)
    variants, conversions = _variants(svg, api, synthetic=True, text=text, width=width)
    return {
        "name": name,
        "variants": variants,
        "normalization": conversions,
        "regions": [(200.0, 200.0, width)],
        "mapping_inputs": {
            "text": text,
            "width": width,
            "baseline": 200.0,
            "x": 200.0,
            "viewport": [1280, 800],
        },
    }


def _render_variant(svg: str, budget: _Budget) -> tuple[dict[str, Any], Image.Image | None]:
    record: dict[str, Any] = {}
    try:
        return record, _stages(svg, budget, record)
    except _ERRORS:
        record.update(_failure())
        return record, None


def _sample_variant(
    record: dict[str, Any],
    image: Image.Image,
    plain: Image.Image,
    regions: list[tuple[float, float, float]],
    api: Any,
) -> bool:
    try:
        record["observations"] = [
            {
                "target": pixel_observations(image, plain, region, api),
                "wrong_cell": pixel_observations(
                    image, plain, (region[0] + region[2] * 3, region[1], region[2]), api
                ),
            }
            for region in regions
        ]
    except _ERRORS:
        record.update(_failure())
        return False
    return True


def _observe_case(case: dict[str, Any], api: Any, budget: _Budget) -> dict[str, Any]:
    result = {key: value for key, value in case.items() if key != "variants"}
    result.update(variants={}, complete=True)
    missing_before = len(budget.missing)
    images: dict[str, Image.Image] = {}
    try:
        for name, svg in case["variants"].items():
            record, image = _render_variant(svg, budget)
            result["variants"][name] = record
            if image is None:
                result["complete"] = False
            else:
                images[name] = image
        for name, image in images.items():
            if "plain" not in images:
                result["complete"] = False
                result["variants"][name]["observations_missing"] = "plain raster unavailable"
                continue
            complete = _sample_variant(
                result["variants"][name], image, images["plain"], case["regions"], api
            )
            result["complete"] = result["complete"] and complete
    finally:
        for image in images.values():
            image.close()
    result["complete"] = result["complete"] and len(budget.missing) == missing_before
    return result


def _case_snapshot(name: str, factory: Any, api: Any, budget: _Budget) -> dict[str, Any]:
    observed: dict[str, Any] = {"name": name, "complete": False}
    try:
        case = factory()
        observed = _observe_case(case, api, budget)
        context = evidence._font_context(case["variants"]["original"])
        font = evidence._comparison_font()
        observed["fonts"] = {
            "context": context,
            "comparison": font,
            "files": evidence._observed_font_files(context, font),
        }
        fonts_complete = font["available"] and observed["fonts"]["files"]["complete"]
        if not fonts_complete:
            observed["missing"] = ["font_observations_incomplete"]
        observed["complete"] = observed["complete"] and fonts_complete
    except _ERRORS:
        observed.update(complete=False, **_failure())
    return observed


def collect_report(api: Any) -> dict[str, Any]:
    budget = _Budget()
    result: dict[str, Any] = {
        "schema_version": 1,
        "complete": True,
        "missing": [],
        "cases": [],
        "qualification": "Observations, not assertion retries or a visual pass",
        "binding": source_binding(),
        "versions": {
            "python": platform.python_version(),
            **{name: evidence._version(name) for name in ("resvg-py", "Pillow", "textual", "rich")},
        },
    }
    factories = [
        (scene, lambda scene=scene: _captured_case(scene, api))
        for scene in ("editor-command-caret", "keyboard-multicursor")
    ]
    factories.extend(
        (
            name,
            lambda name=name, text=text, width=width: _synthetic_case(name, text, width, api),
        )
        for name, text, width in SYNTHETIC_CASES
    )
    for name, factory in factories:
        observed = _case_snapshot(name, factory, api, budget)
        result["cases"].append(observed)
        if "fonts" not in result and "fonts" in observed:
            result["fonts"] = observed["fonts"]
        result["complete"] = result["complete"] and observed["complete"]
    result["missing"] = sorted({*result["missing"], *budget.missing})
    result["observed_byte_totals"] = budget.used
    result["limits"] = {"svg": evidence.MAX_SVG_BYTES, "png": evidence.MAX_COMPARISON_PNG_BYTES}
    result["complete"] = result["complete"] and not result["missing"]
    return result


def write_report(path: Path, report: dict[str, Any]) -> bool:
    return raster_transport.write_report(path, report, max_bytes=MAX_REPORT_BYTES)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    api = importlib.import_module("tests.agent_tools.underline_rasterization")
    report = collect_report(api)
    complete = write_report(arguments.output, report)
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
