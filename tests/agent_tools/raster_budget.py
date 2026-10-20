from __future__ import annotations

import base64
import json
from io import BytesIO
from typing import TYPE_CHECKING, Any

import pytest
from PIL import Image

from agent_tools import sql_explorer_svg_evidence as evidence
from tests.agent_tools._support import raster_assets as assets
from tests.agent_tools._support import raster_observations as observations
from tests.agent_tools._support import raster_transport as transport

if TYPE_CHECKING:
    from pathlib import Path


def test_diagnostic_png_limit_accepts_exact_eight_mib_and_reports_one_byte_over() -> None:
    assert observations.MAX_DIAGNOSTIC_PNG_BYTES == 8 * 1024 * 1024
    assert evidence.MAX_SVG_BYTES == 4 * 1024 * 1024
    assert evidence.MAX_COMPARISON_PNG_BYTES == 4 * 1024 * 1024
    assert transport.MAX_REPORT_BYTES == 8 * 1024 * 1024
    assert transport.MAX_ARTIFACT_BYTES == 64 * 1024 * 1024
    data = b"x" * observations.MAX_DIAGNOSTIC_PNG_BYTES
    budget = observations._Budget()
    exact = budget.retain("png", data)
    assert exact["status"] == "ok"
    assert exact["byte_length"] == len(data)
    assert exact["sha256"] == evidence.digest(data)
    assert base64.b64decode(exact["content"], validate=True) == data
    assert not budget.missing
    excess = budget.retain("png", b"x")
    assert excess["status"] == "omitted"
    assert excess["reason"] == "combined_byte_limit"
    assert "content" not in excess
    assert budget.used == {"svg": 0, "png": len(data) + 1}
    assert budget.missing == ["png:combined_byte_limit"]


@pytest.mark.parametrize("extra", [0, 1])
def test_report_uses_diagnostic_limit_and_preserves_overflow_incompleteness(
    monkeypatch: pytest.MonkeyPatch, extra: int
) -> None:
    count = 0

    def snapshot(name: str, factory: Any, api: Any, budget: Any) -> dict[str, Any]:
        nonlocal count
        data = b"x" * (observations.MAX_DIAGNOSTIC_PNG_BYTES + extra) if count == 0 else b""
        count += 1
        record = budget.retain("png", data)
        return {"name": name, "complete": record["status"] == "ok", "variants": {}}

    monkeypatch.setattr(observations, "_case_snapshot", snapshot)
    monkeypatch.setattr(evidence, "_version", lambda _name: "unavailable")
    report = observations.collect_report(None)
    assert count == 6
    assert report["limits"] == {
        "svg": evidence.MAX_SVG_BYTES,
        "png": observations.MAX_DIAGNOSTIC_PNG_BYTES,
    }
    assert report["observed_byte_totals"]["png"] == observations.MAX_DIAGNOSTIC_PNG_BYTES + extra
    assert report["complete"] is (extra == 0)
    assert report["missing"] == ([] if extra == 0 else ["png:combined_byte_limit"])


def test_duplicate_stages_keep_logical_budget_and_lossless_indexed_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dimensions = {"direct": (3, 2), "intermediate": (6, 4), "reduced": (3, 2)}
    data = {}
    for name, size in dimensions.items():
        with Image.new("RGB", size, "#abcdef") as image, BytesIO() as stream:
            image.save(stream, format="PNG")
            data[name] = stream.getvalue()
    assert data["direct"] == data["reduced"]
    total = sum(len(png) for png in data.values())
    monkeypatch.setattr(observations, "MAX_DIAGNOSTIC_PNG_BYTES", total)
    budget = observations._Budget()
    stages = {
        name: {**budget.retain("png", png), "dimensions": list(dimensions[name])}
        for name, png in data.items()
    }
    assert budget.used["png"] == total
    report = {
        "schema_version": 1,
        "complete": True,
        "binding": {"execution": "test"},
        "observed_byte_totals": dict(budget.used),
        "cases": [{"name": "retained", "complete": True, "variants": {"original": stages}}],
    }
    path = tmp_path / "raster.json"
    assert assets.write_index(path, report, max_bytes=4096)
    index = json.loads(path.read_bytes())
    assets.verify_references(tmp_path, index)
    case = json.loads(assets.read_asset(tmp_path, index["cases"][0]))
    retained = case["variants"]["original"]
    assert set(retained) == set(stages)
    for name, record in retained.items():
        assert record["byte_length"] == len(data[name])
        assert record["sha256"] == evidence.digest(data[name])
        assert record["dimensions"] == list(dimensions[name])
        assert assets.read_asset(tmp_path, record) == data[name]
    assert len(list((tmp_path / "assets").glob("*.png"))) == 2
    assert index["observed_byte_totals"]["png"] == total
    assert all("content" in record for record in stages.values())
    assert not list(tmp_path.rglob("*.tmp"))
    omitted = budget.retain("png", data["direct"])
    assert budget.used["png"] == total + len(data["direct"])
    assert omitted["status"] == "omitted"
    assert "content" not in omitted
    assert budget.missing == ["png:combined_byte_limit"]
