"""Preview or merge a published tab or dashboard into an existing recipe."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

from datalens_sdk import DataLensConfigurationError as SDKConfigurationError
from datalens_sdk import DataLensValidationError

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
from .import_copy import check_copy_key, copy_preflight, place_defaults, relocate
from .import_fidelity import record, wizard_source_issues
from .import_source import explicit_dataset_ids, location
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
    message = "Import collision at " + path + "; existing content was preserved."
    raise DataLensUtilsError(message)


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
        dataset_ids.update(explicit_dataset_ids(entity.data))
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


def collect_dependencies(
    dashboard: Any, selected: set[str], report: dict[str, Any], register: Any, read: Any
) -> Any:
    """One public dependency traversal for new capture and existing-recipe merges."""
    charts: dict[str, Any] = {}
    datasets: dict[str, Any] = {}
    connections: dict[str, Any] = {}
    chart_ids, dataset_ids = tab_dependencies(dashboard, selected)
    scopes = register("chart", chart_ids)
    for identifier in sorted(chart_ids):
        if scopes.get(identifier) == "artifact":
            report["blockers"].append(
                {
                    "resource": "html_page:" + identifier,
                    "operation": "html_page.import",
                    "reason": "HTML authored-source retrieval is unavailable in SDK 3.1/3.2.",
                }
            )
            continue
        entity = read("chart", identifier)
        if entity is not None:
            charts[identifier] = entity
            chart_dependencies(entity, dataset_ids, connections, report)
    register("dataset", dataset_ids)
    for identifier in sorted(dataset_ids):
        entity = read("dataset", identifier)
        if entity is not None:
            datasets[identifier] = entity
            for source in entity.sources:
                if source.connection_id:
                    connections[source.connection_id] = None
    register("connection", set(connections))
    for identifier in sorted(connections):
        connections[identifier] = read("connection", identifier)
    return SimpleNamespace(
        charts=charts,
        datasets=datasets,
        connections={key: value for key, value in connections.items() if value is not None},
    )


def discover_dependencies(
    client: Any, dashboard: Any, selected: set[str], report: dict[str, Any]
) -> Any:
    def register(kind: str, identifiers: set[str]) -> dict[str, str]:
        if not identifiers or kind == "connection":
            return {}
        return {
            entry.id: getattr(entry, "scope", "")
            for entry in client.navigation.get_entries(ids=sorted(identifiers), page_size=100)
        }

    def read(kind: str, identifier: str) -> Any:
        return getattr(client.get, kind)(
            by_id=identifier, **({} if kind == "connection" else {"branch": "published"})
        )

    return collect_dependencies(dashboard, selected, report, register, read)


def import_dataset(client: Any, entity: Any, context: Any) -> None:
    identifier = entity.id
    registry, proposed, report = context.registry, context.proposed, context.report
    dataset_keys, connection_keys = context.dataset_keys, context.connection_keys
    try:
        value, assets = dataset_definition(client, entity, connection_keys)
        if context.mode == "adopt" and location(entity):
            value["location"] = location(entity)
        if any("manual" in source.parameters for source in entity.sources):
            record(
                report,
                "dataset:" + dataset_keys[identifier],
                "source parameters",
                "normalized",
                "Removed API-only manual flag from public factory arguments.",
            )
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
        if context.mode == "copy":
            check_copy_key(registry, "dataset:" + dataset_keys[identifier], context.mode)
            relocate(value, "dataset", identifier, report, "dataset:" + dataset_keys[identifier])
        for feature in ["sources", "fields", "parameters", "joins", "RLS", "filters", "cache"]:
            record(
                report,
                "dataset:" + dataset_keys[identifier],
                feature,
                "preserved",
                "Extracted from published public SDK metadata and compiled locally.",
            )
    except (DataLensUtilsError, KeyError) as error:
        record(report, "dataset:" + identifier, "source metadata", "unsupported", str(error))
        report["blockers"].append({"resource": "dataset:" + identifier, "reason": str(error)})


def check_imported_chart(entity: Any, context: Any, value: Any) -> None:
    dataset_keys, chart_keys = context.dataset_keys, context.chart_keys
    datasets, report, identifier = context.datasets, context.report, entity.id
    if entity.category == "wizard":
        issues = wizard_source_issues(
            context.client,
            entity,
            {dataset_keys[key]: item for key, item in datasets.items()},
            value,
        )
        if issues:
            for issue in issues:
                record(
                    report,
                    "chart:" + chart_keys[identifier],
                    issue,
                    "unsupported",
                    "Source feature is not reproduced by the public recipe builder.",
                )
            message = "Wizard source parity failed: " + ", ".join(issues)
            raise DataLensUtilsError(message)


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
        value, assets = chart_definition(
            entity,
            {dataset_keys[key]: value for key, value in datasets.items()},
            connection_keys,
            next(tab.id for tab in dashboard.tabs if tab.id in selected),
        )
        if context.mode == "adopt" and location(entity):
            value["location"] = location(entity)
        value["key"] = chart_keys[identifier]
        check_imported_chart(entity, context, value)
        if context.mode == "copy":
            check_copy_key(registry, "chart:" + chart_keys[identifier], context.mode)
            relocate(value, "chart", identifier, report, "chart:" + chart_keys[identifier])
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
        features = ["bindings", "authored assets"]
        if entity.category == "wizard":
            features.extend(["colors", "shapes", "formulas", "filters", "formatting", "settings"])
        for feature in features:
            record(
                report,
                "chart:" + chart_keys[identifier],
                feature,
                "preserved",
                "Extracted from published public SDK metadata and compiled locally.",
            )
    except (
        DataLensUtilsError,
        KeyError,
        StopIteration,
        SDKConfigurationError,
        DataLensValidationError,
        TypeError,
    ) as error:
        record(report, "chart:" + identifier, "source metadata", "unsupported", str(error))
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
        place_defaults(ui, context.charts, chart_keys, report)
        proposed.update(ui)
        proposed["configs/DL objects/dashboard.json"] = {
            "settings": settings,
            "global_parameters": parameters,
            "description": entry_description(dashboard) or "",
            "support_description": dashboard.data.get("supportDescription", ""),
            "access_description": dashboard.data.get("accessDescription", ""),
        }
        if context.mode == "copy":
            proposed["configs/DL objects/dashboard.json"]["copy_from"] = dashboard_id
        for feature in [
            "tabs",
            "shared selectors",
            "layout",
            "member wiring",
            "aliases",
            "placement parameters",
            "settings",
        ]:
            record(
                report,
                "dashboard:" + dashboard_id,
                feature,
                "preserved",
                "Extracted from published public SDK metadata and compiled locally.",
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
            local = (
                json.loads(path.read_text())
                if relative.startswith("configs/") and relative.endswith(".json")
                else path.read_text()
            )
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


def validate_import_inputs(dashboard_id: Any, tab_id: Any, mode: str) -> None:
    if mode not in {"adopt", "copy"}:
        message = "Import mode must be adopt or copy."
        raise DataLensConfigurationError(message)
    if (
        not isinstance(dashboard_id, str)
        or not dashboard_id
        or (tab_id is not None and (not isinstance(tab_id, str) or not tab_id))
    ):
        msg = "Import requires a dashboard ID and, for a tab, a nonempty tab ID."
        raise DataLensConfigurationError(msg)


def import_recipe(
    *,
    dashboard_id: str,
    tab_id: str | None = None,
    dry_run: bool = True,
    upgrade_recipe: bool = False,
    mode: str = "adopt",
) -> dict[str, Any]:
    validate_import_inputs(dashboard_id, tab_id, mode)
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
        "mode": mode,
        "provenance": {},
    }
    if legacy and not upgrade_recipe:
        report["blockers"].append(
            {
                "operation": "recipe.upgrade",
                "reason": "Import requires version 2; explicitly request upgrade_recipe.",
            }
        )
        return report
    record(
        report,
        "dashboard:" + dashboard_id,
        "backend inference, numerical results and rendering",
        "unchecked",
        "Import performs metadata reads and offline compilation only.",
    )
    known = known_resources(registry)
    if mode == "copy":
        copy_preflight(registry, report)
    proposed: dict[str, Any] = {}
    charts: dict[str, Any] = {}
    datasets: dict[str, Any] = {}
    connections: dict[str, Any] = {}
    with datalens_client() as client:
        from .import_archive import preserve  # noqa: PLC0415 - Shared extraction cycle.
        from .import_capture import Capture  # noqa: PLC0415 - Shared capture cycle.

        capture = Capture(client, resume=True)
        with capture.failures():
            selected = capture.discover(dashboard_id, [tab_id] if tab_id is not None else None)
            capture.finish()
            dashboard = capture.entities[dashboard_id]
            charts, datasets, connections = (
                {
                    identifier: entity
                    for identifier, entity in capture.entities.items()
                    if capture.kinds[identifier] == kind
                }
                for kind in ("chart", "dataset", "connection")
            )
            report["provenance"] = {
                "dashboard": dashboard.id,
                "charts": sorted(charts),
                "datasets": sorted(datasets),
                "connections": sorted(connections),
            }
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
                identifier: known.get("chart", {}).get(
                    identifier, semantic_key("chart", identifier)
                )
                for identifier in charts
            }
            proposed["configs/DL objects/connections.json"] = {
                connection_keys[key]: connection_definition(value)
                for key, value in connections.items()
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
                charts=charts,
                client=client,
                mode=mode,
            )
            for entity in datasets.values():
                import_dataset(client, entity, context)
            for entity in charts.values():
                import_chart(entity, context)
            import_ui(context)
            preserve(capture, context)
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
        report["request_count"] = state.runtime.get("_request_count", 0)
    return report
