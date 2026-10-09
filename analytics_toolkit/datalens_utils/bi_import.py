"""Preview or merge a published tab or dashboard into an existing recipe."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

from .auth.client import datalens_client
from .bi_import_resources import (
    chart_definition,
    connection_definition,
    dataset_definition,
    retain_dataset_keys,
    semantic_key,
)
from .bi_import_ui import ui_definition
from .deployment import BIProjectDeployment
from .errors import DataLensConfigurationError, DataLensUtilsError
from .recipe import load_registry
from .recipe_io import replace_files, stage_and_validate
from .resources.bi_store import entry_description
from .session import current_session


def _merge_mapping(local: Any, incoming: Any, path: str) -> Any:
    if local == incoming:
        return copy.deepcopy(local)
    if isinstance(local, dict) and isinstance(incoming, dict):
        result = copy.deepcopy(local)
        for key, value in incoming.items():
            result[key] = (
                _merge_mapping(result[key], value, path + "/" + key)
                if key in result
                else copy.deepcopy(value)
            )
        return result
    raise DataLensUtilsError("Import collision at " + path + "; existing content was preserved.")


def _compatible_location(entity: Any, registry: Any) -> None:
    deployment = current_session().deployment
    if isinstance(deployment, BIProjectDeployment) and deployment.target.kind == "workbook":
        target = deployment.target
        identifier = (
            target.value
            if target.reference == "id"
            else registry.resources["workbook:" + target.value].definition.get("id")
        )
        if not identifier:
            checkpoint = current_session().paths.runtime_root / "resources.json"
            state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
            identifier = state.get("resources", {}).get("workbook:" + target.value, {}).get("id")
        valid = identifier and entity.workbook_id == identifier
    else:
        target_path = (
            deployment.target.value
            if isinstance(deployment, BIProjectDeployment)
            else deployment.target_path
        )
        valid = not entity.workbook_id and (entity.key or "").rpartition("/")[
            0
        ] == target_path.rstrip("/")
    if not valid:
        raise DataLensUtilsError(
            "Import resource "
            + entity.id
            + " is outside the target folder/workbook; relocation is unavailable."
        )


def chart_dependencies(
    entity: Any, dataset_ids: set[str], connections: dict[str, Any], report: dict[str, Any]
) -> None:
    identifier = entity.id
    if entity.category == "wizard":
        dataset_ids.update(entity.dataset_ids)
    elif entity.category == "editor":
        try:
            meta = json.loads(entity.data.get("meta", "{}"))
        except (ValueError, TypeError):
            report["blockers"].append(
                {
                    "resource": "chart:" + identifier,
                    "reason": "Dynamic Editor Meta cannot establish dependency closure.",
                }
            )
            return
        dataset_ids.update(meta.get("links", {}).values())
    elif entity.category == "ql":
        connection_id = (entity.connection or {}).get("entryId")
        if connection_id:
            connections[connection_id] = None


def tab_dependencies(dashboard: Any, selected: set[str]) -> tuple[set[str], set[str]]:
    chart_ids: set[str] = set()
    dataset_ids: set[str] = set()
    for tab in dashboard.tabs:
        if tab.id not in selected:
            continue
        for item in (*tab.items, *tab.global_items):
            chart_ids.update(
                value["chartId"] for value in item.data.get("tabs", ()) if value.get("chartId")
            )
        for control in tab.controls:
            for member in control.members:
                if member.source.chart_id:
                    chart_ids.add(member.source.chart_id)
                if member.source.dataset_id:
                    dataset_ids.add(member.source.dataset_id)
    return chart_ids, dataset_ids


def discover_dependencies(
    client: Any, dashboard: Any, selected: set[str], report: dict[str, Any]
) -> Any:
    charts: dict[str, Any] = {}
    datasets: dict[str, Any] = {}
    connections: dict[str, Any] = {}
    chart_ids: set[str] = set()
    dataset_ids: set[str] = set()
    chart_ids, dataset_ids = tab_dependencies(dashboard, selected)
    inventory = (
        {
            entry.id: entry
            for entry in client.navigation.get_entries(ids=sorted(chart_ids), page_size=100)
        }
        if chart_ids
        else {}
    )
    for identifier in sorted(chart_ids):
        entry = inventory.get(identifier)
        if entry is not None and entry.scope == "artifact":
            report["blockers"].append(
                {
                    "resource": "html_page:" + identifier,
                    "operation": "html_page.import",
                    "reason": "HTML authored-source retrieval is unavailable in SDK 3.1/3.2.",
                }
            )
            continue
        entity = client.get.chart(by_id=identifier, branch="published")
        charts[identifier] = entity
        chart_dependencies(entity, dataset_ids, connections, report)
    if dataset_ids:
        list(client.navigation.get_entries(ids=sorted(dataset_ids), page_size=100))
    for identifier in sorted(dataset_ids):
        datasets[identifier] = client.get.dataset(by_id=identifier, branch="published")
        for source in datasets[identifier].sources:
            connections[source.connection_id] = None
    for identifier in connections:
        connections[identifier] = client.get.connection(by_id=identifier)
    return SimpleNamespace(charts=charts, datasets=datasets, connections=connections)


def import_dataset(client: Any, entity: Any, context: Any) -> None:
    identifier = entity.id
    registry, proposed, report = context.registry, context.proposed, context.report
    dataset_keys, connection_keys = context.dataset_keys, context.connection_keys
    try:
        _compatible_location(entity, registry)
        value, assets = dataset_definition(client, entity, connection_keys)
        resource = registry.resources.get("dataset:" + dataset_keys[identifier])
        if resource is not None and resource.definition.get("sources"):
            checkpoint = current_session().paths.runtime_root / "resources.json"
            state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
            retain_dataset_keys(
                entity,
                value,
                resource.definition,
                state.get("resources", {}).get(resource.key, {}),
            )
        proposed["configs/DL objects/datasets.json"][dataset_keys[identifier]] = value
        proposed.update(assets)
        report["fidelity"].append(
            {
                "resource": "dataset:" + dataset_keys[identifier],
                "status": "preserved",
                "features": [
                    "sources",
                    "fields",
                    "parameters",
                    "joins",
                    "RLS",
                    "filters",
                    "cache",
                ],
            }
        )
    except (DataLensUtilsError, KeyError) as error:
        report["blockers"].append({"resource": "dataset:" + identifier, "reason": str(error)})


def import_chart(entity: Any, context: Any) -> None:
    identifier = entity.id
    registry, proposed, report, known = (
        context.registry,
        context.proposed,
        context.report,
        context.known,
    )
    chart_keys, dataset_keys, connection_keys = (
        context.chart_keys,
        context.dataset_keys,
        context.connection_keys,
    )
    datasets, selected, dashboard = context.datasets, context.selected, context.dashboard
    state = current_session()
    try:
        _compatible_location(entity, registry)
        value, assets = chart_definition(
            entity,
            {dataset_keys[key]: value for key, value in datasets.items()},
            connection_keys,
            next(tab.id for tab in dashboard.tabs if tab.id in selected),
        )
        value["key"] = chart_keys[identifier]
        relative = (
            "configs/DL objects/charts/"
            + value["family"]
            + "/"
            + value["type"]
            + "/"
            + chart_keys[identifier]
            + ".json"
        )
        if identifier in known.get("chart", {}):
            relative = next(
                path.relative_to(state.paths.project_root).as_posix()
                for path in (state.paths.project_root / "configs/DL objects/charts").rglob("*.json")
                if json.loads(path.read_text()).get("key", path.stem) == chart_keys[identifier]
            )
        proposed[relative] = value
        proposed.update(assets)
        report["fidelity"].append(
            {
                "resource": "chart:" + chart_keys[identifier],
                "status": "preserved",
                "features": ["bindings", "placements", "authored assets", "parameters"],
            }
        )
    except (DataLensUtilsError, KeyError, StopIteration) as error:
        report["blockers"].append({"resource": "chart:" + identifier, "reason": str(error)})


def import_ui(context: Any) -> None:
    dashboard, selected, proposed, report = (
        context.dashboard,
        context.selected,
        context.proposed,
        context.report,
    )
    chart_keys, dataset_keys, datasets = (
        context.chart_keys,
        context.dataset_keys,
        context.datasets,
    )
    dashboard_id = dashboard.id
    try:
        ui = ui_definition(
            dashboard,
            selected,
            chart_keys,
            dataset_keys,
            {dataset_keys[key]: value for key, value in datasets.items()},
        )
        settings = ui.pop("imported_dashboard_settings")
        parameters = ui.pop("imported_global_parameters")
        proposed.update(ui)
        proposed["configs/DL objects/dashboard.json"] = {
            "settings": settings,
            "global_parameters": parameters,
            "description": entry_description(dashboard) or "",
            "support_description": dashboard.data.get("supportDescription", ""),
            "access_description": dashboard.data.get("accessDescription", ""),
        }
        report["fidelity"].append(
            {
                "resource": "dashboard:" + dashboard_id,
                "status": "preserved",
                "features": [
                    "tabs",
                    "shared selectors",
                    "layout",
                    "member wiring",
                    "aliases",
                    "placement parameters",
                    "settings",
                ],
            }
        )
    except (DataLensUtilsError, KeyError) as error:
        report["blockers"].append({"resource": "dashboard:" + dashboard_id, "reason": str(error)})


def merge_proposed(
    proposed: dict[str, Any], report: dict[str, Any], *, legacy: bool
) -> dict[str, Any]:
    state = current_session()
    merged: dict[str, Any] = {}
    for relative, value in proposed.items():
        path = state.paths.project_root / relative
        merge_file(relative, value, path, merged, report, legacy=legacy)
    return merged


def merge_file(  # noqa: PLR0913 - One atomic file merge and its collision report.
    relative: str,
    value: Any,
    path: Any,
    merged: dict[str, Any],
    report: dict[str, Any],
    *,
    legacy: bool,
) -> None:
    try:
        if path.exists():
            local = json.loads(path.read_text()) if relative.endswith(".json") else path.read_text()
            if legacy and relative in {
                "configs/runtime.json",
                "configs/DL objects/datasets.json",
            }:
                merged[relative] = value
            else:
                merged[relative] = _merge_mapping(local, value, relative)
        else:
            merged[relative] = value
    except DataLensUtilsError as error:
        report["blockers"].append({"file": relative, "reason": str(error)})


def known_resources(registry: Any) -> dict[str, dict[str, str]]:
    state = current_session()
    known: dict[str, dict[str, str]] = {value.kind: {} for value in registry.resources.values()}
    checkpoint = state.paths.runtime_root / "resources.json"
    cache = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    for key, resource in registry.resources.items():
        identifier = resource.definition.get("id") or cache.get("resources", {}).get(
            "dashboard" if key == "dashboard:main" else key, {}
        ).get("id")
        if identifier:
            known[resource.kind][identifier] = key.split(":", 1)[1]
    return known


def prepare_upgrade(context: Any, *, legacy: bool) -> None:
    state = current_session()
    proposed, registry, datasets, report = (
        context.proposed,
        context.registry,
        context.datasets,
        context.report,
    )
    if legacy:
        proposed["configs/runtime.json"] = {
            "schema_version": 2,
            "installation": state.runtime["installation"],
            "request_interval_seconds": state.runtime.get("request_interval_seconds", 1.2),
        }
        # Existing datasets must be explicitly converted using their verified identities.
        for key, resource in registry.resources.items():
            if resource.kind == "dataset" and resource.definition.get("id") not in datasets:
                report["blockers"].append(
                    {
                        "resource": key,
                        "reason": (
                            "Explicit upgrade needs the current dataset in the imported "
                            "dependency closure."
                        ),
                    }
                )


def import_recipe(
    *,
    dashboard_id: str,
    tab_id: str | None = None,
    dry_run: bool = True,
    upgrade_recipe: bool = False,
) -> dict[str, Any]:
    if (
        not isinstance(dashboard_id, str)
        or not dashboard_id
        or (tab_id is not None and (not isinstance(tab_id, str) or not tab_id))
    ):
        msg = "Import requires a dashboard ID and, for a tab, a nonempty tab ID."
        raise DataLensConfigurationError(msg)
    state = current_session()
    registry = load_registry()
    legacy = state.runtime.get("schema_version", 1) == 1
    report: dict[str, Any] = {
        "source_dashboard_id": dashboard_id,
        "source_tab_id": tab_id,
        "dry_run": dry_run,
        "written": False,
        "proposed_files": {},
        "fidelity": [],
        "blockers": [],
    }
    if legacy and not upgrade_recipe:
        report["blockers"].append(
            {
                "operation": "recipe.upgrade",
                "reason": "Import requires version 2; explicitly request upgrade_recipe.",
            }
        )
        return report
    known = known_resources(registry)
    proposed: dict[str, Any] = {}
    charts: dict[str, Any] = {}
    datasets: dict[str, Any] = {}
    connections: dict[str, Any] = {}
    with datalens_client() as client:
        dashboard = client.get.dashboard(by_id=dashboard_id, branch="published")
        selected = {tab.id for tab in dashboard.tabs} if tab_id is None else {tab_id}
        if selected - {tab.id for tab in dashboard.tabs}:
            msg = "Requested tab is absent from the source dashboard."
            raise DataLensConfigurationError(msg)
        if dashboard.saved_id != dashboard.published_id:
            report["blockers"].append(
                {
                    "resource": "dashboard:" + dashboard_id,
                    "reason": "Source dashboard has an unpublished draft.",
                }
            )
        dependencies = discover_dependencies(client, dashboard, selected, report)
        charts, datasets, connections = (
            dependencies.charts,
            dependencies.datasets,
            dependencies.connections,
        )
        connection_keys = {
            identifier: known.get("connection", {}).get(
                identifier, semantic_key("connection", identifier)
            )
            for identifier in connections
        }
        dataset_keys = {
            identifier: known.get("dataset", {}).get(
                identifier, semantic_key("dataset", identifier)
            )
            for identifier in datasets
        }
        chart_keys = {
            identifier: known.get("chart", {}).get(identifier, semantic_key("chart", identifier))
            for identifier in charts
        }
        proposed["configs/DL objects/connections.json"] = {
            connection_keys[key]: connection_definition(value) for key, value in connections.items()
        }
        proposed["configs/DL objects/datasets.json"] = {}
        context = SimpleNamespace(
            registry=registry,
            proposed=proposed,
            report=report,
            known=known,
            dataset_keys=dataset_keys,
            chart_keys=chart_keys,
            connection_keys=connection_keys,
            datasets=datasets,
            selected=selected,
            dashboard=dashboard,
        )
        for entity in datasets.values():
            import_dataset(client, entity, context)
        for entity in charts.values():
            import_chart(entity, context)
        import_ui(context)
        prepare_upgrade(context, legacy=legacy)
        merged = merge_proposed(proposed, report, legacy=legacy)
        report["proposed_files"] = merged
        if not report["blockers"]:
            try:
                stage_and_validate(merged)
            except (DataLensUtilsError, ValueError, TypeError, KeyError) as error:
                report["blockers"].append({"operation": "import.compile", "reason": str(error)})
        if not dry_run and not report["blockers"]:
            replace_files(merged)
            report["written"] = True
    return report
