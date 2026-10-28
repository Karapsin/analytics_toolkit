"""Preserved source provenance for strict imports into an existing recipe."""

from __future__ import annotations

from typing import Any

from .bi_import_resources import semantic_key
from .import_materialize import dependencies
from .import_source import digest, wizard_bindings


def preserve(capture: Any, context: Any) -> None:
    """Retain the shared capture without rebinding a destination dashboard to its source."""
    keys = {
        identifier: kind
        + ":"
        + (
            "main"
            if kind == "dashboard"
            else getattr(context, kind + "_keys", {}).get(
                identifier, semantic_key(kind, identifier)
            )
        )
        for identifier, kind in capture.kinds.items()
    }
    files, resources = capture.source_files(keys)
    identity = digest({"resources": resources, "selected": sorted(context.selected)})[:20]
    prefix = "configs/imports/" + identity + "/"
    names = {relative: prefix + relative for relative in files}
    for identifier, key in keys.items():
        entry = resources[key]
        if entry["source_file"]:
            entry["source_file"] = names[entry["source_file"]]
        entry["dependencies"] = dependencies(capture, identifier, keys)
        entry["asset_hashes"] = {
            names[path]: digest(value)
            for path, value in capture.records.get(identifier, {}).get("assets", {}).items()
        }
        entry["restrictions"] = [
            {
                "feature": "source archive",
                "reason": (
                    "Immutable source provenance; destination configuration uses strict "
                    "import fidelity checks."
                ),
            }
        ]
        entity = capture.entities.get(identifier)
        if entity is not None and getattr(entity, "category", None) == "wizard":
            entry["dataset_bindings"] = wizard_bindings(entity, context.datasets)
    complete = (
        capture.verified and not capture.issues and len(capture.records) == len(capture.kinds)
    )
    manifest = {
        "version": 1,
        "purpose": "import-provenance",
        "source_dashboard_id": context.dashboard.id,
        "selected_tabs": sorted(context.selected),
        "capture_complete": complete,
        "revision_inventory_stable": capture.verified,
        "resources": resources,
        "capture_issues": capture.issues,
        "fidelity": context.report["fidelity"],
    }
    context.proposed.update({names[path]: value for path, value in files.items()})
    manifest_path = prefix + "manifest.json"
    context.proposed[manifest_path] = manifest
    context.report.update(
        capture_complete=complete,
        revision_inventory_stable=capture.verified,
        capture_issues=capture.issues,
        capture_manifest=manifest_path,
        cache_hits=capture.cache_hits,
    )
    if not complete:
        context.report["blockers"].extend(
            capture.issues
            or [
                {
                    "operation": "import.capture",
                    "reason": (
                        "Strict merge requires complete source capture "
                        "and verified published revisions."
                    ),
                }
            ]
        )
