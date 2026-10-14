"""Translate cloud metadata to stable recipe identities at the version-2 pull boundary."""

from __future__ import annotations

import re
from typing import Any

from .bi_datasets import field_handles, source_identities
from .dataset_identity import resolved_definition
from .errors import DataLensUtilsError


def remember_mapping(mapping: dict[str, str], actual: str, original: str) -> None:
    if actual in mapping and mapping[actual] != original:
        message = "Cloud field GUID maps to ambiguous recipe identities: " + actual
        raise DataLensUtilsError(message)
    mapping[actual] = original


def normalize_dataset(incoming: Any, definition: Any) -> None:
    mapping: dict[str, str] = {}
    for key, value in definition.get("fields", {}).items():
        original = value.get("guid")
        if original:
            field = incoming["fields"][key]
            remember_mapping(mapping, field["guid"], original)
            field["guid"] = original
    formula = incoming.get("cache_invalidation", {}).get("field", {}).get("formula", {})
    if isinstance(formula, dict) and isinstance(formula.get("guid_formula"), str):
        formula["guid_formula"] = re.sub(
            r"\[([^\[\]]+)\]",
            lambda match: "[" + mapping.get(match[1], match[1]) + "]",
            formula["guid_formula"],
        )


def normalize_chart(incoming: Any, registry: Any, store: Any, datasets: Any) -> None:
    if incoming["family"] != "wizard" or not (
        incoming.get("aggregated_measures") or incoming.get("hierarchies")
    ):
        return
    role = incoming["dataset"]
    dataset = datasets[role]
    resource = registry.resources["dataset:" + role]
    checkpoint = store.state["resources"].get(store.internal_key(resource.key), {})
    if checkpoint.get("id") != dataset.id:
        checkpoint = {}
    definition = resource.definition
    resolved = resolved_definition(definition, checkpoint)
    sources = source_identities(dataset, resolved, checkpoint)
    handles = field_handles(dataset, resolved, sources)
    mapping: dict[str, str] = {}
    for key, value in definition.get("fields", {}).items():
        original = value.get("guid")
        if original:
            remember_mapping(mapping, handles[key].guid, original)
    for value in incoming.get("aggregated_measures", {}).values():
        value["field"] = mapping.get(value["field"], value["field"])
    local = {value["guid"] for value in incoming.get("local_fields", ())}
    local.update(value["guid"] for value in incoming.get("aggregated_measures", {}).values())
    local.update(value["guid"] for value in incoming.get("hierarchies", {}).values())
    for value in incoming.get("hierarchies", {}).values():
        value["fields"] = [
            guid if guid in local else mapping.get(guid, guid) for guid in value["fields"]
        ]
