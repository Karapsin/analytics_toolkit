"""Verify published resources, export outside the recipe, retain company IDs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from datalens_sdk import recipes

from analytics_toolkit.datalens_utils.dashboard_generation.charts.create import chart_getter
from analytics_toolkit.datalens_utils.dashboard_generation.datasets.create import dataset_issues
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session as session

from .charts import check_chart
from .dashboard import dashboard_issues


def _declared_manual_bindings(contents: Any, definitions: Any) -> Any:
    """QL parameters have no dataset parameter; validate explicit receivers."""
    bindings = set()
    for content in contents.values():
        selectors = list(content.get("selectors", {}).values())
        selectors.extend(
            value
            for group in content.get("selector_groups", {}).values()
            for value in group["definitions"].values()
        )
        for selector in selectors:
            source = selector["source"]
            if source["kind"] != "manual":
                continue
            parameter = source["param_name"]
            receivers = [key for key in selector["recipients"] if key in definitions]
            if receivers and all(
                parameter in {p["name"] for p in definitions[key].get("params", [])}
                and definitions[key]["family"] == "ql"
                for key in receivers
            ):
                bindings.add(selector["key"])
    return bindings


def verify_and_export(  # noqa: PLR0913
    *,
    context: Any,
    dashboard: Any,
    datasets: Any,
    charts: Any,
    dataset_definitions: Any,
    chart_definitions: Any,
    tab_definitions: Any,
    contents: Any,
    description: Any,
    hide_tabs: Any,
    refetch: Any = True,
) -> Any:
    client, resources = context.client, context.resources
    if refetch:
        datasets = {
            role: client.get.dataset(by_id=dataset.id, branch="published")
            for role, dataset in datasets.items()
        }
    for role, dataset in datasets.items():
        definition = dataset_definitions[role]
        resources.verify_location(dataset, definition["name"], scope="dataset")
        issues = dataset_issues(dataset, definition)
        if issues:
            message = f"Published dataset {definition['name']!r}: {issues}"
            raise DataLensUtilsError(message)
    if refetch:
        charts = {
            role: chart_getter(client, chart_definitions[role])(by_id=chart.id, branch="published")
            for role, chart in charts.items()
        }
    for role, chart in charts.items():
        definition = chart_definitions[role]
        resources.verify_location(chart, definition["name"], scope="widget")
        issues = check_chart(chart, context=context, datasets=datasets, definition=definition)
        if issues:
            message = f"Published chart {definition['name']!r}: {issues}"
            raise DataLensUtilsError(message)
    if refetch:
        dashboard = client.get.dashboard(by_id=dashboard.id, branch="published")
    resources.verify_location(dashboard, scope="dash")
    issues = dashboard_issues(
        dashboard,
        tab_definitions=tab_definitions,
        contents=contents,
        datasets=datasets,
        charts=charts,
        chart_definitions=chart_definitions,
        description=description,
        hide_tabs=hide_tabs,
    )
    if issues:
        message = f"Published dashboard does not match its recipe: {issues}"
        raise DataLensUtilsError(message)
    declared = _declared_manual_bindings(contents, chart_definitions)
    issues = [
        issue
        for issue in recipes.validate_dashboard_refs(client, dashboard)
        if not (issue.kind == "unbound_manual_selector" and issue.item_id in declared)
    ]
    if issues:
        message = f"Published resource references are invalid: {issues}"
        raise DataLensUtilsError(message)

    bundle = export_bundle(dashboard, datasets=datasets, charts=charts)
    resources.complete_target()
    context.verified_datasets, context.verified_charts = datasets, charts
    session().emit(f"Verified export: {bundle}")
    return dashboard


def export_bundle(dashboard: Any, *, datasets: Any, charts: Any) -> Any:
    """Export verified published snapshots through public SDK methods.

    Explicit entities include Editor dataset links and the external JS selector,
    and avoid fetching a different dependency revision. No connection is exported.
    """
    entities = {
        "dashboard": dashboard,
        **{f"dataset:{k}": v for k, v in datasets.items()},
        **{f"chart:{k}": v for k, v in charts.items()},
    }
    identity = {
        key: {"id": value.id, "rev_id": value.rev_id, "path": value.key}
        for key, value in sorted(entities.items())
    }
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[
        :16
    ]
    destination = (
        session().paths.runtime_root
        / "artifacts"
        / f"{dashboard.id}-{dashboard.rev_id}-{fingerprint}"
    )
    marker = destination / "manifest.json"
    if marker.is_file():
        manifest = json.loads(marker.read_text(encoding="utf-8"))
        if manifest.get("resources") == identity and all(
            (destination / item).is_file() for item in manifest.get("files", [])
        ):
            return destination
    destination.mkdir(parents=True, exist_ok=True)
    files = []
    for key, entity in entities.items():
        category = "dashboard" if key == "dashboard" else key.split(":", 1)[0] + "s"
        parent = destination / category
        parent.mkdir(parents=True, exist_ok=True)
        output = entity.to_file(parent)
        filename = (
            "dashboard.json"
            if key == "dashboard"
            else "dataset.json"
            if category == "datasets"
            else "chart.json"
        )
        artifact = output / filename
        if not artifact.is_file():
            message = f"SDK export did not create expected artifact: {artifact}"
            raise DataLensUtilsError(message)
        files.append(str(artifact.relative_to(destination)))
    marker.write_text(
        json.dumps({"resources": identity, "files": sorted(files)}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return destination


def _write_config(path: Any, value: Any) -> Any:
    encoded = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if path.read_text(encoding="utf-8") != encoded:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(path)


def record_configured_ids(dashboard: Any, *, datasets: Any, charts: Any) -> Any:
    """Retain verified real IDs in company-shareable JSON."""
    path = session().paths.project_root / "configs/DL objects/datasets.json"
    definitions = json.loads(path.read_text(encoding="utf-8"))
    for key, dataset in datasets.items():
        definitions[key]["id"] = dataset.id
    _write_config(path, definitions)
    for directory in ("charts", "selectors"):
        for path in (session().paths.project_root / "configs/DL objects" / directory).rglob(
            "*.json"
        ):
            definition = json.loads(path.read_text(encoding="utf-8"))
            definition["id"] = charts[definition.get("key", path.stem)].id
            _write_config(path, definition)
    path = session().paths.project_root / "configs/DL objects/dashboard.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["id"] = dashboard.id
    _write_config(path, definition)
