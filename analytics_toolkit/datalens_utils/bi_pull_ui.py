"""Pull managed dashboard metadata without collapsing chart-tab endpoints."""

from __future__ import annotations

import copy
from typing import Any

from .bi_import_ui import ui_definition
from .errors import DataLensUtilsError
from .resources.bi_store import entry_description
from .validation.dashboard import normalize_params


def projection(template: Any, incoming: Any) -> Any:
    """Keep omission semantics while importing values owned by the recipe."""
    if isinstance(template, dict) and isinstance(incoming, dict):
        return {
            key: projection(value, incoming[key])
            for key, value in template.items()
            if key in incoming
        }
    return copy.deepcopy(incoming)


def project_ui_files(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for path, value in base.items():
        if path in incoming and path not in {
            "configs/UI/links/chart_params.json",
            "configs/UI/links/connections.json",
        }:
            result[path] = projection(value, incoming[path])
        elif (
            path.startswith("configs/UI/selectors/")
            and isinstance(value, dict)
            and value.get("key")
        ):
            remote_path = "configs/UI/selectors/" + value["key"] + ".json"
            if remote_path not in incoming:
                raise DataLensUtilsError("Managed selector is missing: " + value["key"])
            result[path] = projection(value, incoming[remote_path])
    return result


def retain_direct_placements(
    base: dict[str, Any], incoming: dict[str, Any], charts: dict[str, Any], result: dict[str, Any]
) -> None:
    """Represent changed direct placements as explicit widgets with stable item IDs."""
    explicit = base.get("configs/UI/widgets.json", {})
    parameters = base.get("configs/UI/links/chart_params.json", {})
    for key, widget in incoming["configs/UI/widgets.json"].items():
        if key in explicit or key not in charts:
            continue
        chart = charts[key]
        changed = (
            widget["title"] != chart["title"]
            or normalize_params(widget["params"]) != normalize_params(parameters.get(key, {}))
            or widget["enable_action_params"] != chart.get("enable_action_params", False)
            or any(
                widget["presentation"].get(name) != value
                for name, value in chart.get("presentation", {}).items()
            )
        )
        if changed:
            result.setdefault("configs/UI/widgets.json", {})[key] = copy.deepcopy(widget)


def retain_action_routes(incoming: dict[str, Any], result: dict[str, Any]) -> None:
    actions = {
        key
        for key, widget in incoming["configs/UI/widgets.json"].items()
        if widget["enable_action_params"]
    }
    actions.update(
        group + "/" + member["key"]
        for group, definition in incoming["configs/UI/chart_groups.json"].items()
        for member in definition["charts"]
        if member["enable_action_params"]
    )
    routes = result["configs/UI/links/connections.json"]
    for sender in actions:
        routes.setdefault(sender, incoming["configs/UI/links/connections.json"].get(sender, []))


def pull_dashboard(
    entity: Any, base: dict[str, Any], datasets: dict[str, Any], charts: dict[str, Any]
) -> dict[str, Any]:
    tabs = set(base["configs/UI/tabs.json"])
    owned = {key for layout in base["configs/UI/layout.json"].values() for key in layout}
    groups = base.get("configs/UI/chart_groups.json", {})
    member_keys = {
        key: [value["key"] for value in group["charts"]] for key, group in groups.items()
    }
    for tab in entity.tabs:
        for item in tab.items:
            if item.id in member_keys and len(item.data.get("tabs", ())) != len(
                member_keys[item.id]
            ):
                message = "Pull cannot infer changed chart-group topology: " + item.id
                raise DataLensUtilsError(message)
    incoming = ui_definition(
        entity,
        tabs,
        {value["id"]: key for key, value in charts.items()},
        {value.id: key for key, value in datasets.items()},
        datasets,
        managed_items=owned,
        member_keys=member_keys,
    )
    settings = incoming.pop("imported_dashboard_settings")
    parameters = incoming.pop("imported_global_parameters")
    result = copy.deepcopy(base)
    result = project_ui_files(base, incoming)
    retain_direct_placements(base, incoming, charts, result)
    # Direct legacy placements retain their parameter file; explicit widgets and
    # groups retain theirs. Recipients stay qualified with stable member keys.
    result["configs/UI/links/connections.json"] = {
        key: incoming["configs/UI/links/connections.json"].get(key, [])
        for key in base.get("configs/UI/links/connections.json", {})
    }
    retain_action_routes(incoming, result)
    for key in base.get("configs/UI/links/chart_params.json", {}):
        widget = incoming["configs/UI/widgets.json"].get(key)
        if widget is not None:
            result["configs/UI/links/chart_params.json"][key] = widget["params"]
    definition = result["definition"]
    for key in definition.get("settings", {}):
        definition["settings"][key] = settings.get(key)
    if "global_parameters" in definition:
        definition["global_parameters"] = parameters
    for key, wire in (
        ("description", "description"),
        ("support_description", "supportDescription"),
        ("access_description", "accessDescription"),
    ):
        if key in definition:
            definition[key] = (
                entry_description(entity) if key == "description" else entity.data.get(wire, "")
            )
    return result
