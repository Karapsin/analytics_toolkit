"""Refresh preserved source without turning unsupported resources into writers."""

from __future__ import annotations

import copy
from typing import Any

from .bi_import import chart_dependencies, tab_dependencies
from .errors import DataLensUtilsError
from .import_source import digest, source_assets, source_document
from .resources.bi_store import safe_metadata


def refresh_source(
    resource: Any, entity: Any, registry: Any, manifest: Any, proposed: Any
) -> dict[str, Any]:
    known = {item.definition.get("id") for item in registry.resources.values()}
    references: set[str] = set()
    if resource.kind == "dashboard":
        charts, datasets = tab_dependencies(entity, {tab.id for tab in entity.tabs})
        references = charts | datasets
    elif resource.kind == "chart":
        connections: dict[str, Any] = {}
        report: dict[str, Any] = {"blockers": []}
        chart_dependencies(entity, references, connections, report)
        references.update(connections)
        if report["blockers"]:
            raise DataLensUtilsError(
                "Cannot establish refreshed dependency closure: " + str(report["blockers"])
            )
    elif resource.kind == "dataset":
        references = {source.connection_id for source in entity.sources if source.connection_id}
    original_references = {
        registry.resources[key].definition.get("id")
        for key in resource.capture.get("dependencies", ())
    }
    if references - known or (
        resource.kind in {"chart", "dataset"} and references != original_references
    ):
        raise DataLensUtilsError(
            "Source dependency closure changed; import into a new project before merging: "
            + str(sorted(references.symmetric_difference(original_references)))
        )
    document = source_document(entity, resource.kind)
    entry: dict[str, Any] = copy.deepcopy(resource.capture)
    if not entry.get("source_file"):
        raise DataLensUtilsError(
            "Previously inaccessible source needs a fresh import: " + resource.key
        )
    if document["location"] != resource.definition.get("location"):
        raise DataLensUtilsError("Source location changed; migration refused: " + resource.key)
    proposed[entry["source_file"]] = document
    assets = source_assets(document)
    proposed.update(assets)
    entry.update(
        source_hash=digest(document),
        metadata=document["metadata"],
        baseline_metadata=safe_metadata(entity, resource.kind),
    )
    entry["asset_hashes"] = {path: digest(value) for path, value in assets.items()}
    manifest["resources"][resource.key] = entry
    return entry
