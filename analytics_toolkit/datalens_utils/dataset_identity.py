"""Retain backend-inferred GUIDs without treating recipe GUIDs as cloud IDs."""

from __future__ import annotations

import copy
import json
from typing import Any, cast

from .errors import DataLensComponentError
from .session import current_session


def check_components(entity: Any, key: str, stage: str) -> None:
    raw = getattr(entity, "raw", {})
    errors = raw.get("dataset", {}).get("component_errors", raw.get("component_errors", {}))
    items = errors.get("items", []) if isinstance(errors, dict) else errors
    if items:
        raise DataLensComponentError(key, stage, entity.id, items)


def resolved_definition(definition: Any, checkpoint: Any) -> Any:
    result = copy.deepcopy(definition)
    originals = checkpoint.get("recipe_field_guids", {})
    for key, field in result.get("fields", {}).items():
        actual = checkpoint.get("field_guids", {}).get(key)
        if actual and (not field.get("guid") or field.get("guid") == originals.get(key)):
            field["guid"] = actual
    mapping = {
        original: checkpoint["field_guids"][key]
        for key, original in originals.items()
        if original and key in checkpoint.get("field_guids", {})
    }
    cache = result.get("cache_invalidation", {}).get("field", {}).get("formula", {})
    if isinstance(cache, dict) and isinstance(cache.get("guid_formula"), str):
        for original, actual in mapping.items():
            cache["guid_formula"] = cache["guid_formula"].replace(
                "[" + original + "]", "[" + actual + "]"
            )
    return result


def resolved_guid(dataset: Any, guid: str) -> str:
    # Only declared original GUIDs map to identities owned by this dataset's checkpoint.
    path = current_session().paths.runtime_root / "resources.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    for entry in state.get("resources", {}).values():
        if entry.get("id") == dataset.id:
            for key, original in entry.get("recipe_field_guids", {}).items():
                if original == guid:
                    return cast("str", entry.get("field_guids", {}).get(key, guid))
    return guid
