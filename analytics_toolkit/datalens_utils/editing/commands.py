from __future__ import annotations

import copy
import json
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

from datalens_sdk import DatasetDataFilter

from analytics_toolkit.datalens_utils.auth.client import datalens_client
from analytics_toolkit.datalens_utils.dashboard_generation.charts.create import (
    chart_getter,
    create_charts,
)
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.configuration import (
    read_contents,
)
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.populate import (
    populate_dashboard,
)
from analytics_toolkit.datalens_utils.dashboard_generation.datasets.create import (
    create_datasets,
    dataset_issues,
)
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.resources.store import ResourceStore
from analytics_toolkit.datalens_utils.session import current_session as session
from analytics_toolkit.datalens_utils.settings import (
    read_chart_definitions,
    read_config,
    source_tables,
)
from analytics_toolkit.datalens_utils.validation.charts import check_chart
from analytics_toolkit.datalens_utils.validation.checks import verify_and_export
from analytics_toolkit.datalens_utils.validation.dashboard import dashboard_issues
from analytics_toolkit.datalens_utils.validation.recipe import (
    validate_recipe,
    validate_resource_name,
)

from .pull import pull_chart, pull_dataset, pull_ui
from .state import EditState, configuration, fingerprint, inventory, merge, write_files


@contextmanager
def phase(name: Any) -> Iterator[Any]:
    started, before = time.perf_counter(), session().runtime.get("_request_count", 0)
    try:
        yield
    finally:
        session().emit(
            f"{name}: {session().runtime.get('_request_count', 0) - before} requests, "
            f"{time.perf_counter() - started:.2f}s"
        )


def deployment_target(deployment: Any) -> Any:
    return {
        key: deployment[key]
        for key in ("organization_id", "target_path", "dashboard_name", "connection_id")
    }


def selection(arguments: Any, units: Any) -> Any:
    if arguments.chart:
        keys = {"chart:" + key for key in arguments.chart}
    elif arguments.dataset:
        keys = {"dataset:" + key for key in arguments.dataset}
    elif arguments.ui:
        keys = {"dashboard"}
    else:
        keys = set(units)
    if keys - units.keys():
        message = f"Unknown resource keys: {sorted(keys - units.keys())}"
        raise DataLensUtilsError(message)
    return keys


def chart_datasets(definition: Any) -> Any:
    return (
        ({definition["dataset"]} if definition.get("dataset") else set())
        | set(definition.get("links", {}).values())
        | {layer["dataset"] for layer in definition.get("layers", []) if layer.get("dataset")}
    )


def context_for(
    client: Any, deployment: Any, entries: Any, dataset_definitions: Any, settings: Any
) -> Any:
    # Incremental commands look up existing folders only. Creation/moves and
    # adoption remain the responsibility of the unchanged full entry point.
    folder = client.get.folder(by_path=deployment["target_path"])
    folders = {"dash": folder}
    for role, scope in (("charts", "widget"), ("datasets", "dataset")):
        folders[scope] = client.get.folder(
            by_path=folder.key.rstrip("/") + "/" + settings["folders"][role]
        )
    target = {
        "organization_id": deployment["organization_id"],
        "folder_path": deployment["target_path"].strip("/"),
        "connection_id": deployment["connection_id"],
        "source_tables": source_tables(dataset_definitions),
    }
    resources = ResourceStore(
        folder,
        target,
        allow_folder_move=True,
        dataset_ids={role: value.get("id") for role, value in dataset_definitions.items()},
        entries=[entry for entry in entries.values() if entry is not None],
    )
    resources.set_folders(
        folders, entries=[entry for entry in entries.values() if entry is not None]
    )
    connection = client.get.connection(by_id=deployment["connection_id"])
    if connection.name != deployment["connection_name"] or connection.type != "clickhouse":
        message = "Configured ClickHouse connection identity does not match."
        raise DataLensUtilsError(message)
    return SimpleNamespace(
        client=client,
        resources=resources,
        connection=connection,
        folder=folder,
        source_tables=source_tables(dataset_definitions),
        entries=entries,
    )


def fetch(client: Any, key: Any, unit: Any, charts: Any, *, branch: Any = "published") -> Any:
    if key == "dashboard":
        return client.get.dashboard(by_id=unit["id"], branch=branch)
    kind, role = key.split(":", 1)
    getter = client.get.dataset if kind == "dataset" else chart_getter(client, charts[role])
    return getter(by_id=unit["id"], branch=branch)


def validate_import(files: Any, context: Any, datasets: Any, fetched: Any) -> Any:
    # Validate the complete proposed project before replacing any user's file.
    # Validators use a staged tree and actual SDK read models; no cloud writes
    # or reconstructed server snapshots are needed.
    with tempfile.TemporaryDirectory(prefix="datalens-pull-") as temporary:
        root = Path(temporary).resolve()
        for directory in ("configs", "assets"):
            if (session().paths.project_root / directory).is_dir():
                shutil.copytree(session().paths.project_root / directory, root / directory)
        for relative, value in files.items():
            path = root / relative
            path.write_text(
                json.dumps(value, ensure_ascii=False) if relative.endswith(".json") else value,
                encoding="utf-8",
            )
        with session().staged(root):
            definitions, charts = read_config("DL objects/datasets.json"), read_chart_definitions()
            tabs, contents = read_config("UI/tabs.json"), read_contents(allow_overlaps=True)
            validate_recipe(definitions, charts, tabs, contents)
            for key, entity in fetched.items():
                if key.startswith("chart:"):
                    issues = check_chart(
                        entity,
                        context=context,
                        datasets=datasets,
                        definition=charts[key.split(":", 1)[1]],
                    )
                elif key.startswith("dataset:"):
                    issues = dataset_issues(entity, definitions[key.split(":", 1)[1]])
                else:
                    references = {role: context.entries["chart:" + role] for role in charts}
                    settings = read_config("DL objects/dashboard.json")
                    issues = dashboard_issues(
                        entity,
                        tab_definitions=tabs,
                        contents=contents,
                        datasets=datasets,
                        charts=references,
                        chart_definitions=charts,
                        description=settings.get("description", ""),
                        hide_tabs=settings.get("hide_tabs", False),
                    )
                if key == "dashboard":
                    geometry = [
                        issue
                        for issue in issues
                        if getattr(issue, "kind", None) in ("overlap", "layout_reflow")
                    ]
                    for issue in geometry:
                        session().emit(f"Imported existing layout warning: {issue.message}")
                    issues = [issue for issue in issues if issue not in geometry]
                if issues:
                    message = (
                        f"{key}"
                        ": remote properties cannot be imported by current adapters: "
                        f"{issues}"
                        ". No files changed."
                    )
                    raise DataLensUtilsError(message)


def full_verify(  # noqa: PLR0913
    context: Any,
    units: Any,
    dataset_definitions: Any,
    chart_definitions: Any,
    settings: Any,
    state: Any,
) -> Any:
    datasets = {
        role: fetch(context.client, "dataset:" + role, units["dataset:" + role], chart_definitions)
        for role in dataset_definitions
    }
    charts = {
        role: fetch(context.client, "chart:" + role, units["chart:" + role], chart_definitions)
        for role in chart_definitions
    }
    dashboard = fetch(context.client, "dashboard", units["dashboard"], chart_definitions)
    entities = {
        "dashboard": dashboard,
        **{"dataset:" + role: value for role, value in datasets.items()},
        **{"chart:" + role: value for role, value in charts.items()},
    }
    dashboard = verify_and_export(
        context=context,
        dashboard=dashboard,
        datasets=datasets,
        charts=charts,
        dataset_definitions=dataset_definitions,
        chart_definitions=chart_definitions,
        tab_definitions=read_config("UI/tabs.json"),
        contents=read_contents(),
        description=settings.get("description", ""),
        hide_tabs=settings.get("hide_tabs", False),
        refetch=False,
    )
    for key, entity in entities.items():
        state.remember(key, units[key], entity)
    state.save()
    return dashboard


def stage_files(
    proposed: Any,
    files: Any,
    units: Any,
    dataset_definitions: Any,
    *,
    reject_conflicts: Any = False,
) -> Any:
    """Expand unit-owned files into one project tree, preserving shared roles."""
    for relative, value in files.items():
        if relative == "chart_titles":
            for role, title in value.items():
                chart_unit = units["chart:" + role]
                path = next(name for name in chart_unit["files"] if name.endswith(".json"))
                definition = copy.deepcopy(proposed.get(path, chart_unit["files"][path]))
                definition["title"] = title
                proposed[path] = definition
        elif relative == "configs/DL objects/datasets.json":
            proposed.setdefault(relative, copy.deepcopy(dataset_definitions)).update(
                copy.deepcopy(value)
            )
        elif reject_conflicts and relative in proposed and proposed[relative] != value:
            message = f"Selected objects disagree on shared file {relative!r}; no files changed."
            raise DataLensUtilsError(message)
        else:
            proposed[relative] = copy.deepcopy(value)


def import_changes(  # noqa: PLR0913
    context: Any,
    arguments: Any,
    selected: Any,
    units: Any,
    state: Any,
    dataset_definitions: Any,
    chart_definitions: Any,
) -> Any:
    branch = arguments.branch
    datasets = {
        role: fetch(
            context.client,
            "dataset:" + role,
            units["dataset:" + role],
            chart_definitions,
            branch=branch,
        )
        for role in dataset_definitions
    }
    proposed: dict[str, Any] = {}
    baselines: dict[str, Any] = {}
    fetched: dict[str, Any] = {}
    remote_files: dict[str, Any] = {}
    for key, unit in units.items():
        base = state.value["resources"].get(key, {}).get("files", unit["files"])
        stage_files(remote_files, base, units, dataset_definitions)
    for key in sorted(selected, key=lambda value: (not value.startswith("dataset:"), value)):
        local = units[key]["files"]
        base = state.value["resources"].get(key, {}).get("files", local)
        incoming = copy.deepcopy(base)
        if key == "dashboard":
            entity = fetch(context.client, key, units[key], chart_definitions, branch=branch)
            incoming = pull_ui(entity, base, datasets, chart_definitions)
        elif key.startswith("dataset:"):
            role = key.split(":", 1)[1]
            entity = datasets[role]
            path = "configs/DL objects/datasets.json"
            incoming[path][role] = pull_dataset(entity, base[path][role])
        else:
            role = key.split(":", 1)[1]
            entity = fetch(context.client, key, units[key], chart_definitions, branch=branch)
            path = next(relative for relative in base if relative.endswith(".json"))
            incoming[path], assets = pull_chart(entity, base[path], datasets, context)
            incoming.update(assets)
        merged = merge(base, local, incoming, key)
        baselines[key], fetched[key] = incoming, entity
        stage_files(proposed, merged, units, dataset_definitions, reject_conflicts=True)
        stage_files(remote_files, incoming, units, dataset_definitions)
    # A retained local change must not be compared with the remote snapshot.
    # First prove that extraction represents the fetched objects; then validate
    # the merged local recipe independently, before replacing any file.
    validate_import(remote_files, context, datasets, fetched)
    validate_import(proposed, context, datasets, {})
    write_files(proposed)
    current = configuration()
    for key, entity in fetched.items():
        state.remember(key, current[key], entity, branch=branch, baseline_files=baselines[key])
    state.save()
    session().emit(
        f"Pulled {len(fetched)} {branch} objects; local edits preserved by three-way merge."
    )


def check_queries(datasets: Any, charts: Any, definitions: Any, affected: Any) -> Any:  # noqa: ARG001
    # Compile affected Wizard dataset projections with real fields and filters.
    # This does not evaluate chart-local formulas or the deployed QL/Editor runtime.
    seen = set()
    for role in affected:
        definition = definitions[role]
        if definition["family"] != "wizard":
            session().emit(
                f"{role}: SQL/JS validated locally; "
                "deployed chart results are outside the public SDK."
            )
            continue
        dataset = datasets[definition["dataset"]]
        names = list(
            dict.fromkeys(
                name for values in definition.get("fields", {}).values() for name in values
            )
        )
        columns = [
            dataset.find_field(name) for name in names if dataset.find_field(name) is not None
        ]
        filters = [
            DatasetDataFilter(
                dataset.fields.by_name(rule["field"]), rule["operation"], rule.get("values", ())
            )
            for rule in definition.get("filters", [])
            if dataset.find_field(rule["field"]) is not None
        ]
        signature = fingerprint([dataset.id, names, definition.get("filters", [])])
        if columns and signature not in seen:
            dataset.get_dataset_data(columns=columns, filters=filters, limit=1)
            seen.add(signature)
        if len(columns) != len(names):
            session().emit(
                f"{role}: chart-local fields checked as metadata; dataset API cannot execute them."
            )
    session().emit(
        f"Dataset query checks: {len(seen)}; query acceptance only, "
        "not comparison against expected business values."
    )


def run(arguments: Any, deployment: Any) -> Any:  # noqa: C901, PLR0912
    started = time.perf_counter()
    session().runtime["_request_count"] = 0
    units = configuration()
    settings = read_config("DL objects/dashboard.json")
    dataset_definitions, chart_definitions = (
        read_config("DL objects/datasets.json"),
        read_chart_definitions(),
    )
    validate_recipe(
        dataset_definitions,
        chart_definitions,
        read_config("UI/tabs.json"),
        read_contents(allow_overlaps=True),
    )
    validate_resource_name(deployment["dashboard_name"])
    state = EditState(deployment_target(deployment))
    with datalens_client() as client:
        with phase("Revision inventory"):
            entries = inventory(client, units)
        changes = state.changes(units, entries, branch=getattr(arguments, "branch", "published"))
        if arguments.command == "status":
            for key, change in changes.items():
                if change != "unchanged":
                    session().emit(f"{key}: {change}")
            session().emit(
                f"{sum(value == 'unchanged' for value in changes.values())}/{len(units)} unchanged"
            )
        else:
            selected = set(units) if arguments.command == "verify" else selection(arguments, units)
            if any(entries[key] is None for key in selected):
                message = (
                    "Selected resources are missing or have no configured ID. Run "
                    "dashboard.py without arguments for full creation/recovery."
                )
                raise DataLensUtilsError(message)
            if arguments.command == "apply" and any(
                changes[key] == "untracked" for key in selected
            ):
                with phase("Initial full baseline"):
                    context = context_for(
                        client, deployment, entries, dataset_definitions, settings
                    )
                    full_verify(
                        context, units, dataset_definitions, chart_definitions, settings, state
                    )
                changes = state.changes(units, entries)
            if arguments.command == "verify":
                with phase("Full published verification"):
                    context = context_for(
                        client, deployment, entries, dataset_definitions, settings
                    )
                    full_verify(
                        context, units, dataset_definitions, chart_definitions, settings, state
                    )
            elif arguments.command == "pull":
                selected = {key for key in selected if changes[key] != "unchanged"}
                changed_datasets = {
                    key.split(":", 1)[1] for key in selected if key.startswith("dataset:")
                }
                if changed_datasets:
                    selected.update(
                        "chart:" + role
                        for role, definition in chart_definitions.items()
                        if chart_datasets(definition) & changed_datasets
                    )
                    selected.add("dashboard")
                if selected:
                    with phase("Pull and local validation"):
                        context = context_for(
                            client, deployment, entries, dataset_definitions, settings
                        )
                        import_changes(
                            context,
                            arguments,
                            selected,
                            units,
                            state,
                            dataset_definitions,
                            chart_definitions,
                        )
                else:
                    session().emit("No remote changes to pull.")
            else:
                selected = {key for key in selected if changes[key] != "unchanged"}
                if selected:
                    with phase("Incremental apply"):
                        apply_changes(
                            client,
                            deployment,
                            entries,
                            selected,
                            changes,
                            units,
                            state,
                            dataset_definitions,
                            chart_definitions,
                            settings,
                        )
                else:
                    session().emit("No changes; no writes.")
    session().emit(
        f"Total: {session().runtime.get('_request_count', 0)} requests, "
        f"{time.perf_counter() - started:.2f}s"
    )
    return {"resource_statuses": changes}


def apply_changes(  # noqa: C901, PLR0913
    client: Any,
    deployment: Any,
    entries: Any,
    selected: Any,
    changes: Any,
    units: Any,
    state: Any,
    dataset_definitions: Any,
    chart_definitions: Any,
    settings: Any,
) -> Any:
    dataset_roles = {key.split(":", 1)[1] for key in selected if key.startswith("dataset:")}
    chart_roles = {key.split(":", 1)[1] for key in selected if key.startswith("chart:")}
    chart_roles.update(
        role
        for role, definition in chart_definitions.items()
        if chart_datasets(definition) & dataset_roles
    )
    selected.update("chart:" + role for role in chart_roles)
    old_ui = state.value["resources"].get("dashboard", {}).get("files", {})
    previous_titles = old_ui.get("chart_titles", {})
    update_ui = (
        "dashboard" in selected
        or bool(dataset_roles)
        or any(
            chart_definitions[role]["title"] != previous_titles.get(role) for role in chart_roles
        )
    )
    if update_ui:
        selected.add("dashboard")
    for key in sorted(selected):
        old, entry = state.value["resources"].get(key, {}), entries.get(key)
        recovering = state.owns_write(key, units[key], entry)
        if entry is None or (
            changes.get(key) in ("remote", "both", "missing", "untracked") and not recovering
        ):
            message = (
                f"{key} changed in DataLens. Pull and resolve it before apply; no writes performed."
            )
            raise DataLensUtilsError(message)
        if (
            entry.saved_id != entry.published_id
            and not recovering
            and (old.get("branch") != "saved" or old.get("saved_id") != entry.saved_id)
        ):
            message = (
                f"{key}"
                " has an unimported saved draft. Use pull --branch saved before "
                "publishing it."
            )
            raise DataLensUtilsError(message)
        if entry.is_locked:
            message = f"{key} is locked by an editor; no writes performed."
            raise DataLensUtilsError(message)
    context = context_for(client, deployment, entries, dataset_definitions, settings)
    needed = (
        set(dataset_definitions)
        if update_ui
        else set().union(*(chart_datasets(chart_definitions[role]) for role in chart_roles))
    )
    needed.update(dataset_roles)
    for role in needed:
        key = "dataset:" + role
        if key not in selected and changes.get(key) in ("remote", "both", "missing"):
            message = (
                f"Dependency {key} changed in DataLens. Pull it before apply; no writes performed."
            )
            raise DataLensUtilsError(message)
    # A changed dataset's transitive consumers need fresh field handles.
    datasets = {
        role: fetch(client, "dataset:" + role, units["dataset:" + role], chart_definitions)
        for role in needed
    }
    state.begin_apply(units, entries, selected)
    with context.resources.record_mutations(state.record_write):
        if dataset_roles:
            datasets.update(
                create_datasets(
                    context=context,
                    definitions={role: dataset_definitions[role] for role in dataset_roles},
                )
            )
        charts = create_charts(
            context=context,
            datasets=datasets,
            definitions={role: chart_definitions[role] for role in chart_roles},
        )
        verified = {"dataset:" + role: datasets[role] for role in dataset_roles}
        verified.update({"chart:" + role: chart for role, chart in charts.items()})
        if update_ui:
            all_charts = {
                role: charts.get(role, entries["chart:" + role]) for role in chart_definitions
            }
            dashboard = fetch(
                client, "dashboard", units["dashboard"], chart_definitions, branch="saved"
            )
            dashboard = populate_dashboard(
                context=context,
                dashboard=dashboard,
                datasets=datasets,
                charts=all_charts,
                tab_definitions=read_config("UI/tabs.json"),
                contents=read_contents(),
                chart_definitions=chart_definitions,
                description=settings.get("description", ""),
                hide_tabs=settings.get("hide_tabs", False),
            )
            verified["dashboard"] = dashboard
        semantic = {
            role
            for role in chart_roles
            if dataset_roles
            or semantic_change(
                units["chart:" + role], state.value["resources"].get("chart:" + role, {})
            )
        }
        check_queries(datasets, charts, chart_definitions, semantic)
    # Reuse adapter post-write fetches. Recheck the published pointer in one
    # batch; a concurrent cloud change invalidates this verification receipt.
    final = inventory(client, units)
    for key, entity in verified.items():
        if (
            final[key] is None
            or final[key].published_id != entity.rev_id
            or final[key].saved_id != entity.saved_id
        ):
            message = (
                f"{key}"
                " changed during verification. Writes are checkpointed; rerun "
                "verification, not creation."
            )
            raise DataLensUtilsError(message)
    for key, entity in verified.items():
        state.remember(key, units[key], entity)
    state.save()
    session().emit(
        f"Verified {len(verified)} affected objects; unrelated resources were not reconciled."
    )


def semantic_change(unit: Any, old: Any) -> Any:
    if not old:
        return True
    keys = ("fields", "local_fields", "filters", "params", "links", "dataset", "layers")

    def content(files: Any) -> Any:
        return {
            relative: (
                {key: value.get(key) for key in keys} if relative.endswith(".json") else value
            )
            for relative, value in files.items()
        }

    return content(unit["files"]) != content(old["files"])


def remember_full(deployment: Any, dashboard: Any, datasets: Any, charts: Any) -> Any:
    state = EditState(deployment_target(deployment), reset=True)
    units = configuration()
    entities = {
        "dashboard": dashboard,
        **{"dataset:" + role: value for role, value in datasets.items()},
        **{"chart:" + role: value for role, value in charts.items()},
    }
    for key, entity in entities.items():
        state.remember(key, units[key], entity)
    state.save()
