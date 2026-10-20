from __future__ import annotations

import base64
import json
from copy import deepcopy
from io import BytesIO
from typing import TYPE_CHECKING, Any

import pytest
from PIL import Image

from tests.agent_tools._support import raster_assets as assets
from tests.agent_tools._support import raster_transport as transport
from tests.agent_tools._support import renderer_probe

if TYPE_CHECKING:
    from pathlib import Path


def _encoded(data: bytes) -> dict[str, Any]:
    return {
        **transport.identity(data),
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
        "status": "ok",
    }


def _report(data: bytes) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "complete": True,
        "binding": {"execution": "actual"},
        "observed_byte_totals": {"svg": 2 * len(data), "png": 0},
        "fonts": {"observed": "same", "renderer_selected": "unknown"},
        "cases": [
            {
                "name": "retained",
                "complete": True,
                "variants": {"original": {"svg": _encoded(data)}},
                "observations": {"pixels": [32, 33], "predicate": False},
                "fonts": {"observed": "same", "renderer_selected": "unknown"},
            },
            {
                "name": "later-failure",
                "complete": False,
                "variants": {"plain": {"svg": _encoded(data)}},
                "traceback": "original renderer failure",
            },
        ],
    }


def test_index_round_trip_retains_exact_assets_observations_and_failure(tmp_path: Path) -> None:
    report = _report(b"<svg> exact whitespace \n</svg>")
    report["complete"] = False
    report["cases"][1]["complete"] = False
    before = deepcopy(report)
    path = tmp_path / "raster.json"
    assert not assets.write_index(path, report, max_bytes=4096)
    index = json.loads(path.read_bytes())
    assert index["format"] == "raster-asset-index/v1"
    assert index["observed_byte_totals"] == report["observed_byte_totals"]
    assert index["complete"] is False
    case = json.loads(assets.read_asset(tmp_path, index["cases"][0]))
    observed = case["variants"]["original"]["svg"]
    assert assets.read_asset(tmp_path, observed) == b"<svg> exact whitespace \n</svg>"
    assert case["observations"] == report["cases"][0]["observations"]
    assert case["fonts"] == index["fonts"]
    later = json.loads(assets.read_asset(tmp_path, index["cases"][1]))
    assert later["traceback"] == "original renderer failure"
    assert later["variants"]["plain"]["svg"]["path"] == observed["path"]
    assets.verify_references(tmp_path, index)
    assert len(list((tmp_path / "assets").glob("*.svg"))) == 1
    assert not list(tmp_path.rglob("*.tmp"))
    assert report == before


@pytest.mark.parametrize("change", ["missing", "corrupt", "length", "hash", "escape"])
def test_index_rejects_missing_corrupt_or_unsafe_references(tmp_path: Path, change: str) -> None:
    report = _report(b"<svg/>")
    assets.write_index(tmp_path / "raster.json", report, max_bytes=4096)
    index = json.loads((tmp_path / "raster.json").read_bytes())
    reference = index["cases"][0]
    file = tmp_path / reference["path"]
    if change == "missing":
        file.unlink()
    elif change == "corrupt":
        file.write_bytes(b"corrupted")
    elif change == "length":
        reference["byte_length"] += 1
    elif change == "hash":
        reference["sha256"] = "0" * 64
    else:
        reference["path"] = "../outside.json"
    with pytest.raises((OSError, ValueError)):
        assets.verify_references(tmp_path, index)


def test_png_dimensions_are_verified_against_actual_retained_bytes(tmp_path: Path) -> None:
    with Image.new("RGB", (3, 2), "black") as image, BytesIO() as stream:
        image.save(stream, format="PNG")
        data = stream.getvalue()
    store = assets._Assets(tmp_path, 4096)
    reference = {**store.put(data, ".png"), "dimensions": [3, 2]}
    assert assets.read_asset(tmp_path, reference) == data
    reference["dimensions"] = [2, 3]
    with pytest.raises(ValueError, match="dimensions"):
        assets.read_asset(tmp_path, reference)


def test_deduplicated_storage_does_not_reset_logical_comparison_accounting(tmp_path: Path) -> None:
    store = assets._Assets(tmp_path, 4)
    first = store.put(b"<svg/>", ".svg")
    used = store.used
    assert store.put(b"<svg/>", ".svg") == first
    assert store.used == used
    report = _report(b"<svg/>")
    assets.write_index(tmp_path / "raster.json", report, max_bytes=4096)
    index = json.loads((tmp_path / "raster.json").read_bytes())
    assert index["observed_byte_totals"]["svg"] == 12


def test_asset_file_and_transport_caps_are_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport, "MAX_REPORT_BYTES", 4)
    monkeypatch.setattr(transport, "MAX_ARTIFACT_BYTES", 9)
    store = assets._Assets(tmp_path, 4)
    assert assets.read_asset(tmp_path, store.put(b"1234", ".svg")) == b"1234"
    assert store.used == 8
    store.put(b"x", ".svg")
    assert store.used == 9
    with pytest.raises(ValueError, match="transport_byte_limit"):
        store.put(b"y", ".svg")
    with pytest.raises(ValueError, match="file_byte_limit"):
        store.put(b"12345", ".svg")


def test_later_asset_failure_preserves_first_case_and_marks_index_incomplete(
    tmp_path: Path,
) -> None:
    report = _report(b"<svg/>")
    report["cases"][1]["variants"]["plain"]["svg"]["sha256"] = "0" * 64
    assert not assets.write_index(tmp_path / "raster.json", report, max_bytes=4096)
    index = json.loads((tmp_path / "raster.json").read_bytes())
    first = json.loads(assets.read_asset(tmp_path, index["cases"][0]))
    assert assets.read_asset(tmp_path, first["variants"]["original"]["svg"]) == b"<svg/>"
    assert not index["cases"][1]["complete"]
    assert "identity_mismatch" in index["cases"][1]["missing"]


def test_atomic_index_failure_preserves_previous_bytes_without_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "raster.json"
    path.write_bytes(b"previous\n")
    original = type(path).replace

    def fail_replace(source: Path, destination: Path) -> Path:
        if destination == path:
            message = "publication interrupted"
            raise OSError(message)
        return original(source, destination)

    monkeypatch.setattr(type(path), "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted"):
        assets.write_index(path, _report(b"<svg/>"), max_bytes=4096)
    assert path.read_bytes() == b"previous\n"
    assert not list(tmp_path.rglob("*.tmp"))


def test_removed_asset_rejects_final_probe_completeness(tmp_path: Path) -> None:
    report = _report(b"<svg/>")
    report["cases"][1]["complete"] = True
    report["cases"][1].pop("traceback")
    assets.write_index(tmp_path / "raster.json", report, max_bytes=4096)
    assert renderer_probe._verify_raster_index(tmp_path)
    index = json.loads((tmp_path / "raster.json").read_bytes())
    (tmp_path / index["cases"][0]["path"]).unlink()
    assert not renderer_probe._verify_raster_index(tmp_path)
    failed = json.loads((tmp_path / "raster.json").read_bytes())
    assert not failed["complete"]
    assert failed["missing"][0].startswith("published_asset_verification:")
