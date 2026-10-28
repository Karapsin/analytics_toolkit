"""Convert captured source to a validated project without losing unreadable features."""

from __future__ import annotations

import copy
import tempfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from .bi_import import chart_dependencies, import_chart, import_dataset, import_ui
from .bi_import_resources import connection_definition, semantic_key
from .errors import DataLensUtilsError
from .import_manifest import MANIFEST
from .import_source import digest, location, wizard_bindings
from .recipe import ResourceRegistry, load_registry
from .recipe_io import publish_project, serialized
from .resources.bi_store import BIResourceStore, safe_metadata
from .session import current_session

if TYPE_CHECKING:
    from .deployment import BIProjectDeployment


def empty_recipe() -> dict[str, Any]:
    result: dict[str, Any] = {
        "configs/" + name + ".json": {}
        for name in (
            "DL objects/connections",
            "DL objects/datasets",
            "DL objects/dashboard",
            "UI/tabs",
            "UI/layout",
            "UI/widgets",
            "UI/chart_groups",
            "UI/titles",
            "UI/texts",
            "UI/links/connections",
            "UI/links/aliases",
            "UI/links/chart_params",
            "UI/selectors/selector_groups",
        )
    }
    result["configs/runtime.json"] = {
        "schema_version": 2,
        "installation": current_session().runtime["installation"],
    }
    return result


def resource_definition(files: dict[str, Any], key: str) -> dict[str, Any] | None:
    kind, name = key.split(":", 1)
    if kind == "dashboard":
        return files.get("configs/DL objects/dashboard.json")
    if kind == "chart":
        return next(
            (
                value
                for path, value in files.items()
                if path.startswith("configs/DL objects/charts/") and value.get("key") == name
            ),
            None,
        )
    result: dict[str, Any] | None = files.get("configs/DL objects/" + kind + "s.json", {}).get(name)
    return result


def put_reference(files: dict[str, Any], key: str, entry: dict[str, Any], entity: Any) -> None:
    kind, name = key.split(":", 1)
    value: dict[str, Any] = {
        "id": entry["id"],
        "source_ref": key,
        "read_only": True,
    }
    if kind != "dashboard":
        value["name"] = (
            (getattr(entity, "name", None) if entity else None)
            or entry["metadata"].get("name")
            or entry["id"]
        )
    target = location(entity) if entity else None
    if target:
        value["location"] = target
    if kind == "connection":
        value.update(mode="existing", connector=getattr(entity, "type", "unavailable"))
    if kind == "chart":
        existing = resource_definition(files, key) or {}
        tab = existing.get("tab") or next(iter(files["configs/UI/tabs.json"]), None)
        if tab is not None:
            value["tab"] = tab
        for path in list(files):
            if path.startswith("configs/DL objects/charts/") and files[path].get("key") == name:
                del files[path]
        value.update(
            key=name,
            title=value["name"],
            family=getattr(entity, "category", "captured"),
            type="reference",
        )
        files["configs/DL objects/charts/captured/" + name + ".json"] = value
    elif kind == "dashboard":
        files["configs/DL objects/dashboard.json"] = value
    else:
        files.setdefault("configs/DL objects/" + kind + "s.json", {})[name] = value


def dependencies(capture: Any, identifier: str, keys: dict[str, str]) -> list[str]:
    entity = capture.entities.get(identifier)
    kind = capture.kinds[identifier]
    if kind == "dashboard":
        return [key for other, key in keys.items() if other != identifier]
    if kind == "dataset" and entity is not None:
        return sorted(
            {
                keys[source.connection_id]
                for source in entity.sources
                if source.connection_id in keys
            }
        )
    if kind == "chart" and entity is not None:
        datasets: set[str] = set()
        connections: dict[str, Any] = {}
        chart_dependencies(entity, datasets, connections, {"blockers": []})
        return sorted({keys[item] for item in datasets | connections.keys() if item in keys})
    return []


def convert(
    capture: Any, dashboard_id: str, selected: set[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    keys = {
        identifier: "dashboard:main"
        if kind == "dashboard"
        else kind + ":" + semantic_key(kind, identifier)
        for identifier, kind in capture.kinds.items()
    }
    files, resources = capture.source_files(keys)
    files.update(empty_recipe())
    dashboard = capture.entities[dashboard_id]
    groups = {
        kind: {
            identifier: entity
            for identifier, entity in capture.entities.items()
            if capture.kinds[identifier] == kind
        }
        for kind in ("chart", "dataset", "connection")
    }
    report: dict[str, Any] = {"blockers": [], "fidelity": []}
    context = SimpleNamespace(
        registry=ResourceRegistry({}),
        proposed=files,
        report=report,
        known={},
        mode="adopt",
        dataset_keys={
            item: keys[item].split(":", 1)[1]
            for item in capture.kinds
            if capture.kinds[item] == "dataset"
        },
        chart_keys={
            item: keys[item].split(":", 1)[1]
            for item in capture.kinds
            if capture.kinds[item] in {"chart", "html_page"}
        },
        connection_keys={
            item: keys[item].split(":", 1)[1]
            for item in capture.kinds
            if capture.kinds[item] == "connection"
        },
        datasets=groups["dataset"],
        charts=groups["chart"],
        selected=selected,
        dashboard=dashboard,
        client=capture.client,
    )
    for identifier, entity in groups["connection"].items():
        files["configs/DL objects/connections.json"][context.connection_keys[identifier]] = (
            connection_definition(entity)
        )
    for entity in groups["dataset"].values():
        import_dataset(capture.client, entity, context)
    for entity in groups["chart"].values():
        try:
            import_chart(entity, context)
        except (AttributeError, ValueError) as error:
            report["blockers"].append(
                {"resource": "chart:" + entity.id, "reason": "Conversion failed: " + str(error)}
            )
        if entity.category == "wizard":
            resources[keys[entity.id]]["dataset_bindings"] = wizard_bindings(
                entity, groups["dataset"]
            )
    import_ui(context)
    for identifier, key in keys.items():
        entry = resources[key]
        entry["dependencies"] = dependencies(capture, identifier, keys)
        entry["restrictions"] = [
            item
            for item in report["blockers"] + capture.issues
            if item.get("resource") in {key, capture.kinds[identifier] + ":" + identifier}
        ]
        entry["asset_hashes"] = {
            path: digest(value)
            for path, value in capture.records.get(identifier, {}).get("assets", {}).items()
        }
        definition = resource_definition(files, key)
        entry["editable"] = bool(
            capture.kinds[identifier] != "connection"
            and definition
            and entry["capture"] == "complete"
            and not entry["restrictions"]
            and capture.verified
        )
        if entry["editable"] and definition is not None:
            definition.update(id=identifier, source_ref=key)
            target = location(capture.entities[identifier])
            if target:
                definition["location"] = target
        else:
            put_reference(files, key, entry, capture.entities.get(identifier))
    # An imported tab selection cannot claim ownership of the unseen dashboard UI.
    if selected != {tab.id for tab in dashboard.tabs}:
        entry = resources["dashboard:main"]
        entry["editable"] = False
        entry["restrictions"].append(
            {
                "feature": "tab scope",
                "reason": "Unselected tabs are preserved as source; dashboard writes are blocked.",
            }
        )
        put_reference(files, "dashboard:main", entry, dashboard)
    manifest = {
        "version": 1,
        "source_dashboard_id": dashboard_id,
        "selected_tabs": [tab.id for tab in dashboard.tabs if tab.id in selected],
        "revision_inventory_stable": capture.verified,
        "resources": resources,
        "capture_issues": capture.issues,
        "fidelity": report["fidelity"],
    }
    files[MANIFEST] = manifest
    return files, manifest


def launcher(auth: Any) -> str:
    deployment = cast("BIProjectDeployment", current_session().deployment)
    return (
        '"""Generated DataLens launcher; all workflow logic lives in the installed package."""\n'
        "import os\nimport sys\nfrom pathlib import Path\n"
        "from analytics_toolkit.datalens_utils import (\n"
        "    BIProjectDeployment, DataLensProject, TargetLocation,\n)\n"
        "from analytics_toolkit.datalens_utils.cli import run_cli\n\n"
        "ROOT = Path(__file__).resolve().parent\n"
        "RUNTIME = Path(os.environ.get('DATALENS_RUNTIME_DIR',\n"
        f"    {str(current_session().paths.runtime_root)!r}))\n"
        "def make_project():\n"
        f"    deployment = BIProjectDeployment({deployment.dashboard_name!r},\n"
        f"        TargetLocation(**{asdict(deployment.target)!r}),\n"
        f"        **{asdict(auth)!r})\n"
        "    return DataLensProject(ROOT, RUNTIME, deployment)\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(run_cli(make_project(), sys.argv[1:]))\n"
    )


def semantic_restrictions(registry: Any, capture: Any) -> list[tuple[str, str]]:
    from .bi_verification import resource_issues  # noqa: PLC0415 - Avoid verification cycle.

    store = BIResourceStore(registry, readonly=True)
    entities = {
        key: capture.entities[resource.definition["id"]]
        for key, resource in registry.resources.items()
        if resource.definition.get("id") in capture.entities
    }
    result = []
    for key, resource in registry.resources.items():
        if not resource.capture["editable"]:
            continue
        try:
            issues = resource_issues(capture.client, store, registry, key, entities)
        except Exception as error:
            cast("Any", error).resource_key = key
            raise
        issues = [issue for issue in issues if issue != "revisions"]
        if issues:
            result.append(
                (key, "Captured source differs from compiled recipe: " + ", ".join(issues))
            )
    return result


def validate_generated(files: dict[str, Any], manifest: dict[str, Any], capture: Any) -> None:
    from .bi_engine import validate_registry  # noqa: PLC0415 - Avoid engine cycle.

    # A conversion restriction belongs to its resource, not to unrelated source capture.
    with tempfile.TemporaryDirectory(prefix="datalens-import-") as name:
        root = Path(name)
        for _ in range(len(manifest["resources"]) + 1):
            for relative, value in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(serialized(relative, value))
            with current_session().staged(root):
                try:
                    registry = load_registry()
                    validate_registry(registry)
                    restrictions = semantic_restrictions(registry, capture)
                except Exception as error:
                    key = getattr(error, "resource_key", None)
                    if (
                        key is None
                        or key not in manifest["resources"]
                        or not manifest["resources"][key]["editable"]
                    ):
                        raise
                    restrictions = [(key, str(error))]
            if not restrictions:
                return
            for key, reason in restrictions:
                entry = manifest["resources"][key]
                entry["editable"] = False
                entry["restrictions"].append({"feature": "local compilation", "reason": reason})
                for feature in manifest["fidelity"]:
                    if feature["resource"] == key and feature["status"] == "preserved":
                        feature.update(status="unsupported", reason=reason)
                # Remove the prior file from staging too when its route changes.
                old_paths = set(files)
                put_reference(files, key, entry, capture.entities.get(entry["id"]))
                for removed in old_paths - files.keys():
                    (root / removed).unlink(missing_ok=True)
    msg = "Import conversion did not converge."
    raise DataLensUtilsError(msg)


def materialize(
    capture: Any, dashboard_id: str, selected: set[str], auth: Any, *, dry_run: bool
) -> dict[str, Any]:
    capture.announce("Convert project")
    files, manifest = convert(capture, dashboard_id, selected)
    capture.announce("Validate project")
    validate_generated(files, manifest, capture)
    resources = manifest["resources"]
    for key, entry in resources.items():
        definition = cast("dict[str, Any]", resource_definition(files, key))
        entry["baseline_definition"] = copy.deepcopy(definition)
        entity = capture.entities.get(entry["id"])
        entry["baseline_metadata"] = (
            safe_metadata(entity, entry["kind"]) if entity is not None else None
        )
        owned_assets = set(definition.get("scripts", {}).values())
        owned_assets.update(
            definition[name] for name in ("query_file", "content_file") if definition.get(name)
        )
        owned_assets.update(
            value["sql_file"]
            for value in definition.get("sources", {}).values()
            if value.get("sql_file")
        )
        if definition.get("cache_invalidation", {}).get("sql_file"):
            owned_assets.add(definition["cache_invalidation"]["sql_file"])
        entry["baseline_assets"] = {
            path: copy.deepcopy(value)
            for path, value in files.items()
            if path in owned_assets
            or (entry["kind"] == "dashboard" and path.startswith("configs/UI/"))
        }
    coverage = {
        "resources": {
            kind: sum(item["kind"] == kind for item in resources.values())
            for kind in sorted(set(capture.kinds.values()))
        },
        "captured": sum(item["capture"] == "complete" for item in resources.values()),
        "editable": sum(item["editable"] for item in resources.values()),
        "tabs": manifest["selected_tabs"],
    }
    files["configs/coverage.json"] = coverage
    files["dashboard.py"] = launcher(auth)
    files["README.md"] = (
        "# Imported DataLens project\n\n"
        "Published source and supported configuration are retained separately.\n"
        "See configs/import.json for per-resource capture and editing capabilities.\n"
        "Run `python dashboard.py validate --json` for offline integrity and config checks.\n"
        "Use `python dashboard.py status --json` for metadata and\n"
        "`python dashboard.py verify --full --json` for published source/recipe verification.\n"
        "Use plan before explicitly authorized scoped apply; captured-only resources\n"
        "cannot be written. Private state stays in DATALENS_RUNTIME_DIR outside this project.\n"
        "No business data or rendering was verified.\n"
    )
    report = {
        "source_dashboard_id": dashboard_id,
        "destination": str(current_session().paths.project_root),
        "written": False,
        "coverage": coverage,
        "capture_complete": coverage["captured"] == len(resources) and not capture.issues,
        "revision_inventory_stable": capture.verified,
        "resources": resources,
        "fidelity": manifest["fidelity"],
        "capture_issues": capture.issues,
        "proposed_files": sorted(files),
        "blockers": [],
        "cloud_mutations": 0,
    }
    if not dry_run:
        capture.announce("Write project")
        publish_project(files)
        report["written"] = True
    return report
