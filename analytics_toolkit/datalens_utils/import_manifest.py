"""Captured-resource integrity and write restrictions shared by every operation."""

from __future__ import annotations

import json
from typing import Any

from .errors import DataLensConfigurationError, DataLensUtilsError
from .import_source import digest
from .session import current_session
from .settings import asset_path

MANIFEST = "configs/import.json"


def load_manifest() -> dict[str, Any]:
    path = current_session().paths.project_root / MANIFEST
    if not path.exists():
        return {}
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1 or not isinstance(value.get("resources"), dict):
        msg = "Unsupported or malformed import manifest."
        raise DataLensConfigurationError(msg)
    return value


def source(entry: dict[str, Any]) -> dict[str, Any] | None:
    if entry.get("source_file") is None:
        return None
    value: dict[str, Any] = json.loads(asset_path(entry["source_file"]).read_text(encoding="utf-8"))
    if (
        digest(value) != entry.get("source_hash")
        or value.get("metadata", {}).get("id") != entry["id"]
    ):
        raise DataLensConfigurationError("Captured source integrity differs: " + entry["id"])
    return value


def validate_capture(key: str, definition: dict[str, Any], entry: dict[str, Any]) -> None:
    if definition.get("id") != entry.get("id") or definition.get("source_ref") != key:
        raise DataLensConfigurationError("Imported identity/source reference differs: " + key)
    value = source(entry)
    if value and definition.get("location") != value.get("location"):
        raise DataLensConfigurationError(
            "Imported resource location changed; migration refused: " + key
        )
    if not entry.get("editable") and not definition.get("read_only"):
        raise DataLensConfigurationError(
            "Captured-only resource lost its read-only declaration: " + key
        )
    for relative, expected in entry.get("asset_hashes", {}).items():
        # These are preserved original assets, distinct from editable recipe assets.
        if digest(asset_path(relative).read_bytes().decode("utf-8")) != expected:
            raise DataLensConfigurationError("Captured asset integrity differs: " + relative)


def is_reference(resource: Any) -> bool:
    return bool(
        resource.definition.get("read_only")
        or (resource.capture and not resource.capture.get("editable"))
    )


def restriction(resource: Any) -> dict[str, Any]:
    return {
        "resource": resource.key,
        "operation": resource.kind + ".update",
        "status": "blocked",
        "reason": "Captured-only resource cannot be written faithfully through the public adapter.",
        "details": (resource.capture or {}).get("restrictions", []),
    }


def guard_write(resource: Any) -> None:
    if is_reference(resource) and not existing_connection(resource):
        raise DataLensUtilsError("Captured source write blocked: " + str(restriction(resource)))


def existing_connection(resource: Any) -> bool:
    return bool(resource.kind == "connection" and resource.definition.get("mode") == "existing")


def resource_capture(manifest: dict[str, Any], key: str, definition: dict[str, Any]) -> Any:
    entry = manifest.get("resources", {}).get(key)
    if definition.get("source_ref") and entry is None:
        raise DataLensConfigurationError("Missing import manifest entry: " + key)
    if entry:
        validate_capture(key, definition, entry)
    return entry
