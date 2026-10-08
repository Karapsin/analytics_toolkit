"""Reconcile managed dashboard content, selectors, aliases, and ignore edges."""

from __future__ import annotations

from typing import Any

from datalens_sdk import DashboardTab

from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.validation.dashboard import (
    actual_alias_groups,
    alias_groups,
    dashboard_issues,
    definition_items,
    expected_edges,
    item_issues,
    managed_edges,
    managed_ids,
    normalize_params,
    selector_default,
)


def _selector_options(definition: Any, datasets: Any) -> Any:
    source = definition["source"]
    options = {**definition["control"], "title": definition["title"]}
    default = options.get("default_value")
    if isinstance(default, dict):
        options["default_value"] = selector_default(default)
    if source["kind"] == "dataset":
        dataset = datasets[source["dataset"]]
        options.update(dataset=dataset, field=dataset.fields.by_name(source["field"]))
    else:
        options["param_name"] = source["param_name"]
    return options


def add_item(builder: Any, *, tab: Any, item_id: Any, definition: Any, datasets: Any) -> Any:
    options = {"item_id": item_id, "at": tuple(definition["at"])}
    scope = {} if tab is None else {"tab": tab}
    kind = definition["kind"]
    if kind == "chart":
        builder.add_chart(
            definition["chart"].id,
            title=definition["title"],
            params=normalize_params(definition.get("params", {})),
            **options,
            **scope,
        )
    elif kind == "external_selector":
        builder.add_selector(
            chart=definition["chart"].id, title=definition["title"], **options, **scope
        )
    elif kind == "selector_group":
        for member_id in definition["members"]:
            builder.add_selector(
                item_id=member_id,
                group=item_id,
                **_selector_options(definition["definitions"][member_id], datasets),
            )
        builder.add_group_selector(
            group=item_id,
            **options,
            **scope,
            **{
                key: definition.get(key, default)
                for key, default in (
                    ("apply_button", False),
                    ("reset_button", False),
                    ("update_on_change", True),
                    ("show_group_name", False),
                    ("auto_height", False),
                )
            },
        )
    elif kind == "title":
        builder.add_title(definition["text"], size=definition.get("size", "m"), **options, **scope)
    else:
        builder.add_text(definition["text"], **options, **scope)


def _item_location(dashboard: Any, item_id: Any) -> Any:
    """Locate local or global wrappers by item or member identity."""
    matches: list[Any] = []
    for tab in dashboard.tabs:
        matches.extend(
            (tab.id, item.id) for item in (*tab.items, *tab.global_items) if item.id == item_id
        )
        matches.extend(
            (tab.id, control.id)
            for control in tab.controls
            for member in control.members
            if member.id == item_id
        )
    return list(dict.fromkeys(matches))


def _reconcile_wiring(update: Any, dashboard: Any, contents: Any, datasets: Any) -> Any:
    changed = False
    known_fields = {field.guid for dataset in datasets.values() for field in dataset.fields}
    for tab in dashboard.tabs:
        if tab.id not in contents:
            continue
        content = contents[tab.id]
        expected, actual = expected_edges(content), managed_edges(tab, content)
        for logical in set(actual) - expected:
            source, target = actual[logical]
            update.remove_connection(from_item=source, to_item=target, tab=tab.id)
            changed = True
        for source, target in expected - set(actual):
            update.add_connection(from_item=source, to_item=target, tab=tab.id)
            changed = True
        wanted_aliases = alias_groups(content, datasets)
        existing_aliases = actual_alias_groups(tab)
        for group in existing_aliases - wanted_aliases:
            if group.issubset(known_fields):
                update.remove_alias(*sorted(group), tab=tab.id)
                changed = True
        for group in wanted_aliases - existing_aliases:
            update.add_alias(*sorted(group), tab=tab.id)
            changed = True
    return changed


def patch_item(update: Any, tab: Any, item_id: Any, definition: Any, datasets: Any) -> Any:  # noqa: C901, PLR0912
    # Only use point setters when they cover every difference. Everything else
    # follows the existing recoverable removal/restoration path.
    issues = item_issues(tab, item_id, definition, datasets)
    actions: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    if "placement" in issues:
        x, y, w, h = definition["at"]
        actions += [
            ("move_item", (item_id,), {"x": x, "y": y}),
            ("resize_item", (item_id,), {"w": w, "h": h}),
        ]
        issues.remove("placement")
    if definition["kind"] == "chart" and "chart widget parameters" in issues:
        actions.append(
            (
                "set_chart_params",
                (),
                {
                    "item_id": item_id,
                    "params": normalize_params(definition.get("params", {})),
                    "merge": False,
                },
            )
        )
        issues.remove("chart widget parameters")
    if definition["kind"] == "selector_group":
        controls = [control for control in tab.controls if control.id == item_id]
        if len(controls) == 1:
            for member in controls[0].members:
                wanted = definition["definitions"].get(member.id)
                if not wanted:
                    return False
                control, source = wanted["control"], member.source
                if (
                    source.element_type != control["element"]
                    or source.multiselect != control.get("multiselect", False)
                    or source.is_range != control.get("is_range", False)
                    or any(
                        source.raw.get(wire, default) != control.get(public, default)
                        for public, wire, default in (
                            ("show_title", "showTitle", True),
                            ("title_placement", "titlePlacement", "left"),
                            ("inner_title", "innerTitle", None),
                        )
                    )
                ):
                    return False
                allowed = {
                    f"{member.id}: selector identity or title",
                    f"{member.id}: selector default",
                    f"{member.id}: selector control settings",
                }
                if not allowed.intersection(issues):
                    continue
                if member.source_type != wanted["source"]["kind"]:
                    return False
                kwargs = {
                    "item_id": member.id,
                    "title": wanted["title"],
                    "default_value": selector_default(control.get("default_value")),
                    "required": control.get("required", False),
                    "hint": control.get("hint"),
                }
                if source.operation != control.get("operation"):
                    if control.get("operation") is None:
                        return False
                    kwargs["operation"] = control["operation"]
                actions.append(("update_selector", (), kwargs))
                issues = [issue for issue in issues if issue not in allowed]
    if issues:
        return False
    for method, args, kwargs in actions:
        getattr(update, method)(*args, **kwargs)
    return bool(actions)


def populate_dashboard(  # noqa: C901, PLR0912, PLR0913, PLR0915
    *,
    context: Any,
    dashboard: Any,
    datasets: Any,
    charts: Any,
    tab_definitions: Any,
    contents: Any,
    chart_definitions: Any,
    description: Any,
    hide_tabs: Any,
) -> Any:
    client, resources = context.client, context.resources
    dashboard = client.get.dashboard(by_id=dashboard.id, branch="saved")
    unexpected = [
        tab.id for tab in dashboard.tabs if not tab.hidden and tab.id not in tab_definitions
    ]
    if unexpected:
        message = (
            "Unexpected visible tabs are preserved; reconcile them explicitly "
            "before applying the three-tab gallery: "
            f"{unexpected}"
        )
        raise DataLensUtilsError(message)
    expected = {
        role: definition_items(contents.get(role, {}), charts, chart_definitions)
        for role in tab_definitions
    }
    managed = set().union(*(managed_ids(items) for items in expected.values()))
    if sum(len(managed_ids(items)) for items in expected.values()) != len(managed):
        message = "Dashboard item, wrapper, and member keys must be unique across tabs."
        raise DataLensUtilsError(message)
    state = resources.state["resources"]["dashboard"]
    previous_managed = set(state.get("managed_items", ()))
    pending = set(state.get("pending_items", ()))
    tabs = {tab.id: tab for tab in dashboard.tabs}
    remove_ids: set[str] = set()
    layout_changed = False
    for role, items in expected.items():
        for item_id, definition in items.items():
            locations = _item_location(dashboard, item_id)
            problems = (
                item_issues(tabs[role], item_id, definition, datasets)
                if locations and role in tabs
                else []
            )
            layout_changed = layout_changed or "placement" in problems
            point_update = (
                len(locations) == 1
                and locations[0][0] == role
                and problems
                and patch_item(dashboard.update, tabs[role], item_id, definition, datasets)
            )
            if locations and (
                len(locations) != 1 or locations[0][0] != role or (problems and not point_update)
            ):
                remove_ids.update(wrapper for _, wrapper in locations)
                pending.update(managed_ids({item_id: definition}))
    for item_id in previous_managed - managed:
        remove_ids.update(wrapper for _, wrapper in _item_location(dashboard, item_id))
    if remove_ids:
        resources.phase(
            "dashboard",
            "rebuilding",
            pending_items=sorted(pending),
            managed_items=sorted(previous_managed | managed),
        )
        # Removed reserved IDs cannot be reused in the same update. Save the
        # removal as a draft, re-fetch it, then restore the stable item/member IDs.
        dashboard = client.get.dashboard(by_id=dashboard.id, branch="saved")
        update = dashboard.update
        for item_id in sorted(remove_ids):
            update.remove_item(item_id)
        dashboard = resources.persisted(
            "dashboard", update.mode("save").execute(), client.get.dashboard, branch="saved"
        )

    if remove_ids:
        dashboard = client.get.dashboard(by_id=dashboard.id, branch="saved")
    tabs = {tab.id: tab for tab in dashboard.tabs}
    update, changed = dashboard.update, False
    for role, definition in tab_definitions.items():
        tab = tabs.get(role)
        if tab is None:
            template = DashboardTab(definition["title"], tab_id=role, hidden=definition["hidden"])
            for item_id, item in expected[role].items():
                add_item(template, tab=None, item_id=item_id, definition=item, datasets=datasets)
            for source, target in expected_edges(contents[role]):
                template.add_connection(from_item=source, to_item=target)
            for group in alias_groups(contents[role], datasets):
                template.add_alias(*sorted(group))
            update.add_tab(template)
            changed = True
        else:
            if tab.title != definition["title"] or tab.hidden != definition["hidden"]:
                update.update_tab(role, title=definition["title"], hidden=definition["hidden"])
                changed = True
            for item_id, item in expected[role].items():
                if not _item_location(dashboard, item_id):
                    add_item(update, tab=role, item_id=item_id, definition=item, datasets=datasets)
                    changed = True
                elif item_issues(tab, item_id, item, datasets):
                    changed = patch_item(update, tab, item_id, item, datasets) or changed
    # Existing tabs' added widgets/members are immediately addressable by the
    # public update builder; wires can be created in this same save transaction.
    changed = _reconcile_wiring(update, dashboard, contents, datasets) or changed
    if dashboard.data.get("settings", {}).get("hideTabs", False) != hide_tabs:
        update.settings(hide_tabs=hide_tabs)
        changed = True
    if (dashboard.raw.get("annotation") or {}).get("description", "") != description:
        update.description(description)
        changed = True
    if changed:
        resources.phase(
            "dashboard",
            "populating",
            pending_items=sorted(pending),
            managed_items=sorted(previous_managed | managed),
        )
        mode = "save" if remove_ids or layout_changed else "publish"
        dashboard = resources.persisted(
            "dashboard",
            update.mode(mode).execute(),
            client.get.dashboard,
            branch="saved" if mode == "save" else "published",
        )
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
        message = f"Saved dashboard does not match its recipe: {issues}"
        raise DataLensUtilsError(message)
    if dashboard.is_draft:
        dashboard = resources.persisted(
            "dashboard", dashboard.publish_revision(), client.get.dashboard
        )
    resources.phase("dashboard", "published", pending_items=[], managed_items=sorted(managed))
    return dashboard
