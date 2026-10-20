"""Lossless indexed raster evidence within the existing artifact admission limits."""

from __future__ import annotations

import base64
import json
import struct
from copy import deepcopy
from pathlib import Path
from typing import Any

from tests.agent_tools._support import raster_transport as transport


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode("utf-8")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class _Assets:
    def __init__(self, root: Path, report_limit: int) -> None:
        self.root = root
        self.used = report_limit  # Reserve the complete index before admitting assets.
        self.paths: set[str] = set()

    def put(self, data: bytes, suffix: str) -> dict[str, Any]:
        identity = transport.identity(data)
        relative = "assets/" + identity["sha256"] + suffix
        record = {**identity, "encoding": "asset-file", "path": relative}
        if len(data) > transport.MAX_REPORT_BYTES:
            message = "asset_file_byte_limit"
            raise ValueError(message)
        if relative not in self.paths:
            if self.used + len(data) > transport.MAX_ARTIFACT_BYTES:
                message = "asset_transport_byte_limit"
                raise ValueError(message)
            atomic_write(self.root / relative, data)
            self.paths.add(relative)
            self.used += len(data)
        return record

    def externalize(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.externalize(item) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("encoding") == "base64" and "content" in value:
            data = base64.b64decode(value["content"], validate=True)
            if transport.identity(data) != {key: value[key] for key in ("byte_length", "sha256")}:
                message = "encoded_asset_identity_mismatch"
                raise ValueError(message)
            suffix = ".png" if data.startswith(b"\x89PNG\r\n\x1a\n") else ".svg"
            retained = {key: item for key, item in value.items() if key != "content"}
            retained.update(self.put(data, suffix))
            return retained
        result = {}
        for key, item in value.items():
            retained = self.externalize(item)
            if key == "fonts":
                retained = {
                    **self.put(_json_bytes(retained), ".json"),
                    "format": "raster-metadata/v1",
                }
            result[key] = retained
        return result


def read_asset(root: Path, record: dict[str, Any]) -> bytes:
    """Validate the relative hash-addressed reference before consuming any bytes."""
    relative = Path(record["path"])
    expected = record["sha256"]
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "assets"
        or relative.stem != expected
        or len(expected) != 64
        or any(character not in "0123456789abcdef" for character in expected)
        or relative.suffix not in {".svg", ".png", ".json"}
    ):
        message = "invalid_asset_reference"
        raise ValueError(message)
    path = root / relative
    if path.is_symlink() or (root / "assets").is_symlink():
        message = "symlink_asset_reference"
        raise ValueError(message)
    if path.stat().st_size > transport.MAX_REPORT_BYTES:
        message = "asset_file_byte_limit"
        raise ValueError(message)
    data = path.read_bytes()
    if transport.identity(data) != {key: record[key] for key in ("byte_length", "sha256")}:
        message = "asset_identity_mismatch"
        raise ValueError(message)
    if "dimensions" in record:
        if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
            message = "invalid_asset_png"
            raise ValueError(message)
        if list(struct.unpack_from(">II", data, 16)) != record["dimensions"]:
            message = "asset_dimensions_mismatch"
            raise ValueError(message)
    return data


def verify_references(root: Path, value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            verify_references(root, item)
    elif isinstance(value, dict):
        if value.get("encoding") == "asset-file":
            data = read_asset(root, value)
            if value.get("format") in {"raster-case/v1", "raster-metadata/v1"}:
                verify_references(root, json.loads(data))
        else:
            for item in value.values():
                verify_references(root, item)


def _write_case(assets: _Assets, case: dict[str, Any]) -> dict[str, Any]:
    try:
        retained = assets.externalize(case)
        reference = assets.put(_json_bytes(retained), ".json")
        return {
            **reference,
            "format": "raster-case/v1",
            "name": case["name"],
            "complete": case["complete"],
        }
    except (OSError, ValueError, KeyError) as exc:
        return {"name": case["name"], "complete": False, "missing": str(exc)}


def write_index(path: Path, report: dict[str, Any], *, max_bytes: int) -> bool:
    """Keep observations intact; asset deduplication never alters comparison accounting."""
    index = deepcopy(report)
    index["format"] = "raster-asset-index/v1"
    index["complete"] = report["complete"] and all(case["complete"] for case in report["cases"])
    assets = _Assets(path.parent, max_bytes)
    if "fonts" in index:
        try:
            index["fonts"] = {
                **assets.put(_json_bytes(index["fonts"]), ".json"),
                "format": "raster-metadata/v1",
            }
        except (OSError, ValueError) as exc:
            index["complete"] = False
            index.setdefault("missing", []).append("font_metadata:" + str(exc))
    index["cases"] = [_write_case(assets, case) for case in report["cases"]]
    index["complete"] = index["complete"] and all(case["complete"] for case in index["cases"])
    try:
        verify_references(path.parent, index)
    except (OSError, ValueError, KeyError) as exc:
        index["complete"] = False
        index.setdefault("missing", []).append("asset_verification:" + str(exc))
    index["asset_storage"] = {
        "files": len(assets.paths),
        "bytes_including_index_reservation": assets.used,
        "comparison_accounting": "Unchanged logical totals; deduplication affects storage only",
    }
    return transport.write_report(path, index, max_bytes=max_bytes)
