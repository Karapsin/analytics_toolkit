"""Explicit copy ownership and relocation, without cloud writes during import."""

from __future__ import annotations

import json
from typing import Any

from .errors import DataLensUtilsError
from .import_fidelity import record
from .session import current_session


def copy_preflight(registry: Any, report: Any) -> None:
    checkpoint = current_session().paths.runtime_root / "resources.json"
    if checkpoint.exists() and json.loads(checkpoint.read_text()).get("resources"):
        report["blockers"].append(
            {
                "operation": "import.copy",
                "reason": "Copy requires a separate, unused destination runtime.",
            }
        )
    if registry.resources["dashboard:main"].definition.get("id"):
        report["blockers"].append(
            {
                "operation": "import.copy",
                "reason": "Copy destination dashboard must not retain an existing ID.",
            }
        )


def relocate(value: Any, kind: str, source_id: str, report: Any, key: str) -> None:
    value.pop("id", None)
    value["copy_from"] = source_id
    if kind == "dataset":
        for source in value["sources"].values():
            source.pop("id", None)
            source.pop("avatar_id", None)
        for relation in value.get("relations", {}).values():
            relation.pop("id", None)
    elif value.get("family") == "editor":
        value["meta_links_mode"] = "managed"
    record(
        report,
        key,
        "persistent identity",
        "normalized",
        "Copy creates new persistent IDs; source ID is provenance only.",
    )


def chart_defaults(chart: Any) -> dict[str, Any]:
    if chart.category != "wizard":
        return {}
    data, raw = chart.data.get("params"), chart.raw.get("params")
    if data is not None and raw is not None and data != raw:
        message = "Ambiguous Wizard parameter defaults."
        raise DataLensUtilsError(message)
    result = data if data is not None else raw
    if result is None:
        return {}
    if not isinstance(result, dict):
        message = "Wizard defaults must be a parameter mapping."
        raise DataLensUtilsError(message)
    if any(not isinstance(key, str) or not key for key in result):
        message = "Wizard default names must be nonempty strings."
        raise DataLensUtilsError(message)
    return result


def place_defaults(ui: Any, charts: Any, chart_keys: Any, report: Any) -> None:
    defaults = {chart_keys[key]: chart_defaults(value) for key, value in charts.items()}
    placements = list(ui["configs/UI/widgets.json"].values())
    placements.extend(
        member
        for group in ui["configs/UI/chart_groups.json"].values()
        for member in group["charts"]
    )
    for placement in placements:
        values = defaults.get(placement["chart"], {})
        if values:
            placement["params"] = {**values, **placement.get("params", {})}
    for key, values in defaults.items():
        if values:
            record(
                report,
                "chart:" + key,
                "parameter defaults",
                "normalized",
                "Transferred to all placements; explicit placement parameters win. "
                "Dashboard globals and selectors retain runtime precedence.",
            )


def check_copy_key(registry: Any, key: str, mode: str) -> None:
    if mode == "copy" and key in registry.resources:
        message = "Copy semantic key already exists in the destination: " + key
        raise DataLensUtilsError(message)
