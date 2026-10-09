"""Shared selector display, directed recipients, and typed dashboard presentation."""

from __future__ import annotations

import json
import math
from typing import Any

from datalens_sdk import REMOVE_PARAM, Dashboard, DashboardChartTab, DashboardTab

from .capabilities import get_capabilities, require_capability
from .dashboard_generation.dashboard.configuration import (
    _validate_positions,
    read_selector_definitions,
)
from .dashboard_generation.dashboard.populate import _selector_options
from .editing.state import fingerprint
from .errors import DataLensConfigurationError, DataLensUtilsError
from .recipe import strict
from .session import current_session
from .settings import read_config
from .validation.dashboard import _member_issues, actual_alias_groups, normalize_params

POSITION_COMPONENTS = 4
GRID_COLUMNS = 36

SETTINGS = {
    "silent_loading": "silentLoading",
    "dependent_selectors": "dependentSelectors",
    "expand_toc": "expandTOC",
    "hide_dash_title": "hideDashTitle",
    "hide_tabs": "hideTabs",
    "autoupdate_interval": "autoupdateInterval",
    "max_concurrent_requests": "maxConcurrentRequests",
    "load_priority": "loadPriority",
}
_SELECTOR_FIELDS = {"key", "tab", "group", "title", "source", "control", "show_on_tabs", "affects"}
_GROUP_FIELDS = {
    "tab",
    "members",
    "show_on_tabs",
    "apply_button",
    "reset_button",
    "update_on_change",
    "show_group_name",
    "auto_height",
    "border_radius",
}
_PRESENTATION = {
    "show_title",
    "auto_height",
    "background",
    "description",
    "hint",
    "border_radius",
    "pinned",
}
_ACTION_TYPES = {
    "line",
    "area",
    "area_100p",
    "bar",
    "bar_100p",
    "column",
    "column_100p",
    "pie",
    "donut",
    "scatter",
    "flat_table",
    "pivot_table",
    "geolayer",
    "combined_chart",
}


def optional_ui(name: str) -> dict[str, Any]:
    path = current_session().paths.project_root / "configs/UI" / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def grid_position(value: Any, context: str) -> list[int]:
    """Accept whole-number floats without rounding or changing authored JSON."""
    if not isinstance(value, list) or len(value) != POSITION_COMPONENTS:
        message = "Layout requires four numeric coordinates: " + context
        raise DataLensConfigurationError(message)
    if any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in value):
        message = "Layout coordinates must be numbers: " + context
        raise DataLensConfigurationError(message)
    if any(not float(number).is_integer() for number in value):
        require_capability(get_capabilities(), "dashboard.layout.fractional")
    return [int(number) for number in value]


def persisted_layout_issues(dashboard: Any) -> list[Any]:
    """Account for public read views coercing valid numeric geometry to floats."""
    positions = {
        (tab.id, value.item_id): (value.x, value.y, value.w, value.h)
        for tab in dashboard.tabs
        for value in tab.layout
    }
    result = []
    for issue in dashboard.validate():
        coordinates = positions.get((issue.tab_id, issue.item_id))
        numeric = coordinates is not None and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in coordinates
        )
        if (
            issue.kind == "out_of_grid"
            and "non-integer layout geometry" in issue.message
            and numeric
            and coordinates is not None
        ):
            x, y, width, height = coordinates
            if x >= 0 and y >= 0 and width > 0 and height > 0 and x + width <= GRID_COLUMNS:
                continue
        result.append(issue)
    return result


def scope_tabs(
    scope: Any, origin: str, tabs: dict[str, Any], *, influence: bool = False
) -> tuple[str, ...]:
    if scope in ("as_group", "current"):
        return (origin,)
    if scope == ("all_tabs" if influence else "all"):
        return tuple(tabs)
    if (
        not isinstance(scope, list)
        or not scope
        or len(scope) != len(set(scope))
        or set(scope) - tabs.keys()
    ):
        msg = "Selector scope must name known tabs or use a supported scope string."
        raise DataLensConfigurationError(msg)
    return tuple(scope)


def validate_widgets(
    charts: dict[str, Any], ui: dict[str, Any], declared: dict[str, set[str]]
) -> None:
    tabs = ui["tabs"]
    widgets = ui["widgets"]
    for key, widget in widgets.items():
        strict(
            widget,
            {"chart", "tab", "title", "params", "enable_action_params", "presentation"},
            "widget " + key,
        )
        if widget.get("chart") not in charts or widget.get("tab") not in tabs:
            msg = "Widget requires known chart and tab."
            raise DataLensConfigurationError(msg)
        declared[widget["tab"]].add(key)


def validate_chart_groups(
    charts: dict[str, Any], ui: dict[str, Any], declared: dict[str, set[str]]
) -> set[str]:
    tabs = ui["tabs"]
    chart_groups = ui["chart_groups"]
    grouped_charts: set[str] = set()
    for key, group in chart_groups.items():
        strict(group, {"tab", "charts", "presentation"}, "chart group " + key)
        if group.get("tab") not in tabs or not group.get("charts"):
            msg = "Chart groups require a known tab and members."
            raise DataLensConfigurationError(msg)
        declared[group["tab"]].add(key)
        for member in group["charts"]:
            strict(
                member,
                {
                    "key",
                    "chart",
                    "title",
                    "default",
                    "params",
                    "enable_action_params",
                    "description",
                    "hint",
                    "auto_height",
                },
                "chart group member",
            )
            if member.get("chart", member.get("key")) not in charts or not member.get("key"):
                msg = "Unknown or repeated chart group member."
                raise DataLensConfigurationError(msg)
            grouped_charts.add(member.get("chart", member["key"]))
        if len({member["key"] for member in group["charts"]}) != len(group["charts"]):
            msg = "Chart group member keys must be unique."
            raise DataLensConfigurationError(msg)
    return grouped_charts


def declare_charts(
    charts: dict[str, Any], ui: dict[str, Any], declared: dict[str, set[str]]
) -> None:
    tabs = ui["tabs"]
    widgets = ui["widgets"]
    grouped_charts = ui["grouped_charts"]
    for key, chart in charts.items():
        tab = chart.get("tab", chart["family"])
        if tab not in tabs:
            raise DataLensConfigurationError("Chart references an unknown business tab: " + key)
        if (
            chart["type"] != "selector"
            and key not in grouped_charts
            and key not in {value["chart"] for value in widgets.values()}
        ):
            declared[tab].add(key)
    for kind in ("titles.json", "texts.json"):
        for tab, values in optional_ui(kind).items():
            if tab not in tabs:
                msg = "Presentation references an unknown tab."
                raise DataLensConfigurationError(msg)
            declared[tab].update(values)


def selector_wrappers(ui: dict[str, Any]) -> dict[str, Any]:
    tabs = ui["tabs"]
    selectors = ui["selectors"]
    groups = ui["groups"]
    wiring = ui["wiring"]
    wrappers = {}
    for key, definition in selectors.items():
        strict(definition, _SELECTOR_FIELDS, "selector " + key)
        if definition.get("tab") not in tabs:
            msg = "Selector references an unknown origin tab."
            raise DataLensConfigurationError(msg)
        if (
            key not in wiring
            or not isinstance(wiring[key], list)
            or len(wiring[key]) != len(set(wiring[key]))
        ):
            msg = "Each selector requires a unique recipient list."
            raise DataLensConfigurationError(msg)
        definition["recipients"] = wiring[key]
        if definition.get("group"):
            group = groups.get(definition["group"])
            if (
                group is None
                or key not in group.get("members", [])
                or definition["tab"] != group["tab"]
                or definition.get("show_on_tabs", "current") != "current"
            ):
                msg = "Invalid selector group membership or member display scope."
                raise DataLensConfigurationError(msg)
            continue
        if definition.get("affects", "as_group") != "as_group":
            msg = "Standalone selectors inherit their display influence."
            raise DataLensConfigurationError(msg)
        wrapper = key if definition["source"]["kind"] == "editor" else key + "_control"
        display = scope_tabs(definition.get("show_on_tabs", "current"), definition["tab"], tabs)
        if definition["source"]["kind"] == "editor" and display != (definition["tab"],):
            msg = "External Editor selectors support only local display."
            raise DataLensConfigurationError(msg)
        wrappers[wrapper] = {
            "tab": definition["tab"],
            "members": [key],
            "show_on_tabs": definition.get("show_on_tabs", "current"),
            "display": display,
        }
    return wrappers


def group_wrappers(ui: dict[str, Any], wrappers: dict[str, Any]) -> None:
    tabs = ui["tabs"]
    selectors = ui["selectors"]
    groups = ui["groups"]
    for key, group in groups.items():
        strict(group, _GROUP_FIELDS, "selector group " + key)
        if (
            group.get("tab") not in tabs
            or not group.get("members")
            or len(group["members"]) != len(set(group["members"]))
        ):
            msg = "Selector group needs known origin and unique members."
            raise DataLensConfigurationError(msg)
        for member in group["members"]:
            if member not in selectors or selectors[member].get("group") != key:
                msg = "Invalid selector group member."
                raise DataLensConfigurationError(msg)
        wrappers[key] = {
            **group,
            "display": scope_tabs(group.get("show_on_tabs", "current"), group["tab"], tabs),
        }


def validate_wiring(charts: dict[str, Any], ui: dict[str, Any]) -> None:
    chart_groups = ui["chart_groups"]
    selectors = ui["selectors"]
    widgets = ui["widgets"]
    wiring = ui["wiring"]
    all_widgets = (
        set(charts)
        | set(chart_groups)
        | set(selectors)
        | set(widgets)
        | {
            group + "/" + value["key"]
            for group, values in chart_groups.items()
            for value in values["charts"]
        }
    )
    for sender, recipients in wiring.items():
        if sender not in all_widgets or set(recipients) - all_widgets:
            msg = "Unknown recipient/sender wiring identity."
            raise DataLensConfigurationError(msg)
    action_sources = [
        (key, key) for key, chart in charts.items() if chart.get("enable_action_params")
    ]
    action_sources.extend(
        (key, widget["chart"])
        for key, widget in widgets.items()
        if widget.get("enable_action_params")
    )
    action_sources.extend(
        (key + "/" + member["key"], member.get("chart", member["key"]))
        for key, group in chart_groups.items()
        for member in group["charts"]
        if member.get("enable_action_params")
    )
    for reference, key in action_sources:
        chart = charts[key]
        if (chart["family"] == "wizard" and chart["type"] not in _ACTION_TYPES) or chart[
            "family"
        ] not in {"wizard", "editor"}:
            raise DataLensConfigurationError(
                "Chart family/type cannot emit supported action parameters: " + key
            )
        if reference not in wiring and key not in wiring:
            raise DataLensConfigurationError(
                "Action parameters require explicit recipient wiring: " + reference
            )


def load_ui(charts: dict[str, Any]) -> dict[str, Any]:
    tabs = read_config("UI/tabs.json")
    layout = {
        tab: {key: grid_position(value, tab + "/" + key) for key, value in positions.items()}
        for tab, positions in read_config("UI/layout.json").items()
    }
    selectors = read_selector_definitions()
    groups = optional_ui("selectors/selector_groups.json")
    chart_groups = optional_ui("chart_groups.json")
    widgets = optional_ui("widgets.json")
    wiring = optional_ui("links/connections.json")
    ui = {
        "tabs": tabs,
        "layout": layout,
        "selectors": selectors,
        "groups": groups,
        "chart_groups": chart_groups,
        "widgets": widgets,
        "wiring": wiring,
    }
    declared: dict[str, set[str]] = {tab: set() for tab in tabs}
    for tab, definition in tabs.items():
        strict(definition, {"title", "hidden", "preserve_layout"}, "tab " + tab)
    validate_widgets(charts, ui, declared)
    ui["grouped_charts"] = validate_chart_groups(charts, ui, declared)
    declare_charts(charts, ui, declared)
    wrappers = selector_wrappers(ui)
    group_wrappers(ui, wrappers)
    ui["wrappers"] = wrappers
    for key, wrapper in wrappers.items():
        for tab in wrapper["display"]:
            declared[tab].add(key)
    if set(layout) != set(tabs):
        msg = "Every tab requires an explicit layout."
        raise DataLensConfigurationError(msg)
    for tab, positions in layout.items():
        _validate_positions(tab, positions, allow_overlaps=tabs[tab].get("preserve_layout", False))
        if set(positions) != declared[tab]:
            raise DataLensConfigurationError(
                "Layout must position every local/shared item exactly once: " + tab
            )
    validate_wiring(charts, ui)
    return ui


def add_tab_content(
    builder: Any, tab: str, positions: dict[str, Any], resources: dict[str, Any]
) -> None:
    ui, charts, definitions = resources["ui"], resources["charts"], resources["definitions"]
    for kind in ("titles", "texts"):
        for key, value in optional_ui(kind + ".json").get(tab, {}).items():
            getattr(builder, "add_title" if kind == "titles" else "add_text")(
                value["text"],
                tab=tab,
                item_id=key,
                at=tuple(positions[key]),
                **{name: item for name, item in value.items() if name != "text"},
            )
    for key, chart in definitions.items():
        if (
            key not in positions
            or key in ui["grouped_charts"]
            or key in {value["chart"] for value in ui["widgets"].values()}
            or chart["type"] == "selector"
        ):
            continue
        presentation = chart.get("presentation", {})
        strict(presentation, _PRESENTATION, "widget presentation")
        builder.add_chart(
            charts[key].id,
            tab=tab,
            item_id=key,
            title=chart["title"],
            at=tuple(positions[key]),
            params=normalize_params(optional_ui("links/chart_params.json").get(key, {})),
            enable_action_params=chart.get("enable_action_params", False),
            **presentation,
        )
    for key, widget in ui["widgets"].items():
        if widget["tab"] == tab:
            definition = definitions[widget["chart"]]
            builder.add_chart(
                charts[widget["chart"]].id,
                tab=tab,
                item_id=key,
                title=widget.get("title", definition["title"]),
                at=tuple(positions[key]),
                params=normalize_params(widget.get("params", {})),
                enable_action_params=widget.get("enable_action_params", False),
                **widget.get("presentation", {}),
            )
    add_chart_groups(builder, tab, positions, resources)


def add_chart_groups(
    builder: Any, tab: str, positions: dict[str, Any], resources: dict[str, Any]
) -> None:
    ui, charts, definitions = resources["ui"], resources["charts"], resources["definitions"]
    for key, group in ui["chart_groups"].items():
        if group["tab"] != tab:
            continue
        builder.add_chart_group(
            [
                DashboardChartTab(
                    chart=charts[value.get("chart", value["key"])].id,
                    title=value.get(
                        "title", definitions[value.get("chart", value["key"])]["title"]
                    ),
                    **{
                        name: item
                        for name, item in value.items()
                        if name not in {"key", "chart", "title"}
                    },
                )
                for value in group["charts"]
            ],
            tab=tab,
            item_id=key,
            at=tuple(positions[key]),
            **group.get("presentation", {}),
        )


def add_ui(
    builder: Any,
    ui: dict[str, Any],
    charts: dict[str, Any],
    datasets: dict[str, Any],
    definitions: dict[str, Any],
) -> None:
    resources = {"ui": ui, "charts": charts, "definitions": definitions}
    for tab, positions in ui["layout"].items():
        add_tab_content(builder, tab, positions, resources)
    for wrapper, group in ui["wrappers"].items():
        tab = group["tab"]
        for member_id in group["members"]:
            definition = ui["selectors"][member_id]
            if definition["source"]["kind"] == "editor":
                builder.add_selector(
                    chart=charts[definition["source"]["chart"]].id,
                    tab=tab,
                    item_id=member_id,
                    title=definition["title"],
                    at=tuple(ui["layout"][tab][wrapper]),
                )
                continue
            options = _selector_options(definition, datasets)
            builder.add_selector(
                group=wrapper,
                item_id=member_id,
                affects=tuple(definition["affects"])
                if isinstance(definition.get("affects"), list)
                else definition.get("affects", "as_group"),
                **options,
            )
        if ui["selectors"][group["members"][0]]["source"]["kind"] == "editor":
            continue
        display = group.get("show_on_tabs", "current")
        builder.add_group_selector(
            tab=tab,
            group=wrapper,
            item_id=wrapper,
            at=tuple(ui["layout"][tab][wrapper]),
            show_on_tabs=tuple(display) if isinstance(display, list) else display,
            **{
                key: value
                for key, value in group.items()
                if key not in {"tab", "members", "show_on_tabs", "display"}
            },
        )
    for tab, positions in ui["layout"].items():
        builder.apply_layout({key: tuple(value) for key, value in positions.items()}, tab=tab)


def preflight_ui(  # noqa: PLR0913 - Typed dashboard context and dependency snapshots.
    client: Any,
    ui: dict[str, Any],
    charts: dict[str, Any],
    datasets: dict[str, Any],
    definitions: dict[str, Any],
    settings: dict[str, Any],
) -> None:
    """Compile public update operations, endpoint identities and settings offline."""

    data = {
        "tabs": [
            {"id": key, "title": value["title"], "items": [], "layout": []}
            for key, value in ui["tabs"].items()
        ]
    }
    update = Dashboard(id="offline", data=data, installation=client.INSTALLATION).update
    add_ui(update, ui, charts, datasets, definitions)
    endpoints = update_endpoints(ui, update)
    for tab in ui["tabs"]:
        for receiver, sender in wiring_edges(ui, tab, definitions, endpoints):
            update.add_connection(from_item=receiver, to_item=sender, tab=tab)
    strict(settings.get("settings", {}), set(SETTINGS), "dashboard settings")
    configure_dashboard_settings(update, settings, ui)
    update.to_spec()


def endpoint_map(ui: dict[str, Any], items: Any) -> dict[str, tuple[str, ...]]:
    """Resolve stable placement references to public chart-tab/member IDs."""
    result = {}
    for item in items:
        tabs = getattr(item, "tabs", None)
        if tabs is None:
            tabs = getattr(item, "data", {}).get("tabs", ())
        identifiers = tuple(value.id if hasattr(value, "id") else value["id"] for value in tabs)
        if identifiers:
            result[item.id] = identifiers
            if item.id in ui["chart_groups"]:
                members = ui["chart_groups"][item.id]["charts"]
                if len(members) != len(identifiers):
                    msg = "Chart group member count changed."
                    raise DataLensUtilsError(msg)
                for member, identifier in zip(members, identifiers):
                    result[item.id + "/" + member["key"]] = (identifier,)
                    # Preserve the original shorthand when the reference is unambiguous.
                    reference = member["key"]
                    if (
                        reference not in result
                        and sum(
                            member["key"] == reference
                            for group in ui["chart_groups"].values()
                            for member in group["charts"]
                        )
                        == 1
                    ):
                        result[reference] = (identifier,)
        if item.id in ui["wrappers"]:
            members = ui["wrappers"][item.id]["members"]
            result[item.id] = tuple(members)
            result.update({member: (member,) for member in members})
        elif item.id in ui["selectors"]:
            result[item.id] = (item.id,)
    return result


def sender_reaches_tab(sender: str, tab: str, ui: dict[str, Any], receivers: set[str]) -> bool:
    visible = set(ui["layout"][tab])
    if sender in ui["selectors"]:
        selector = ui["selectors"][sender]
        wrapper = next(group for group in ui["wrappers"].values() if sender in group["members"])
        influence = (
            wrapper["display"]
            if selector.get("affects", "as_group") == "as_group"
            else scope_tabs(selector["affects"], selector["tab"], ui["tabs"], influence=True)
        )
        if tab not in influence:
            return False
    elif sender not in receivers and sender not in visible:
        return False
    return True


def wiring_edges(
    ui: dict[str, Any],
    tab: str,
    definitions: dict[str, Any],
    endpoints: dict[str, tuple[str, ...]] | None = None,
) -> set[tuple[str, str]]:
    visible = set(ui["layout"][tab])
    selectors = {
        member
        for group in ui["wrappers"].values()
        if tab in group["display"]
        for member in group["members"]
    }
    receivers = {key for key in visible if key in definitions or key in ui["widgets"]} | selectors
    for key, group in ui["chart_groups"].items():
        if key in visible:
            receivers.discard(key)
            receivers.update(key + "/" + member["key"] for member in group["charts"])
    mapping = endpoints or {key: (key,) for key in receivers}
    edges = set()
    for sender, recipients in ui["wiring"].items():
        if not sender_reaches_tab(sender, tab, ui, receivers):
            continue
        accepted = {
            endpoint
            for recipient in recipients
            for endpoint in mapping.get(recipient, (recipient,))
        }
        sender_ids = mapping.get(sender, (sender,))
        for receiver in receivers:
            for receiver_id in mapping.get(receiver, (receiver,)):
                for sender_id in sender_ids:
                    if receiver_id not in accepted and receiver_id != sender_id:
                        edges.add((receiver_id, sender_id))
    return edges


def update_endpoints(ui: dict[str, Any], builder: Any) -> dict[str, tuple[str, ...]]:
    items: list[Any] = []
    for operation in builder.to_spec().ops:
        items.extend(getattr(operation, "items", ()))
        if hasattr(operation, "tab"):
            items.extend(operation.tab.items)
    return endpoint_map(ui, items)


def retained_routes(dashboard: Any, ui: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """Keep unmanaged edges as logical references while chart-tab IDs are rebuilt."""
    previous_ui = {
        **ui,
        "chart_groups": old.get("baseline", {}).get(
            "configs/UI/chart_groups.json", ui["chart_groups"]
        ),
    }
    result = {}
    for tab in dashboard.tabs:
        mapping = endpoint_map(previous_ui, (*tab.items, *tab.global_items))
        reverse = {
            value: key
            for key, values in mapping.items()
            for value in values
            if "/" in key or key in ui["widgets"] or key in ui["selectors"]
        }
        for item in (*tab.items, *tab.global_items):
            if item.id not in previous_ui["chart_groups"]:
                reverse.update(dict.fromkeys(mapping.get(item.id, ()), item.id))
        managed_senders = {value for key in ui["wiring"] for value in mapping.get(key, (key,))}
        owned = {tuple(edge) for edge in old.get("managed_connections", {}).get(tab.id, ())}
        edges = []
        for edge in tab.connections:
            pair = (edge["from"], edge["to"])
            if pair in owned or (
                "managed_connections" not in old
                and pair[1] in managed_senders
                and pair[0] in reverse
            ):
                continue
            edges.append([reverse.get(value, value) for value in pair])
        result[tab.id] = edges
    return result


def prepare_rebuild(
    client: Any, store: Any, dashboard: Any, ui: dict[str, Any], old: dict[str, Any]
) -> tuple[Any, dict[str, Any]]:
    """Remove managed occurrences in an owned draft before reusing their IDs."""
    rebuilding = old.get("dashboard_rebuild")
    if rebuilding is not None and old.get("pending_write", {}).get("fingerprint") == fingerprint(
        store.registry.resources["dashboard:main"].files
    ):
        dashboard = client.get.dashboard(by_id=dashboard.id, branch="saved")
        store.check_write("dashboard:main", dashboard)
        return dashboard, rebuilding
    declared = set().union(*(set(value) for value in ui["layout"].values()))
    managed = set(old.get("managed_items", ())) | declared
    aliases = {
        tab.id: [sorted(group) for group in actual_alias_groups(tab)] for tab in dashboard.tabs
    }
    rebuilding = {"aliases": aliases, "routes": retained_routes(dashboard, ui, old)}
    update = dashboard.update
    present = {item.id for tab in dashboard.tabs for item in (*tab.items, *tab.global_items)}
    removed = sorted(managed & present)
    if removed:
        for item_id in removed:
            update.remove_item(item_id)
        store.pending_metadata["dashboard"] = {"dashboard_rebuild": rebuilding}
        dashboard = store.persisted(
            "dashboard", update.mode("save").execute(), client.get.dashboard, branch="saved"
        )
    return dashboard, rebuilding


def wire_dashboard(
    update: Any, ui: dict[str, Any], rebuilding: dict[str, Any], resources: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    definitions, datasets, endpoints = (
        resources["definitions"],
        resources["datasets"],
        resources["endpoints"],
    )
    original_aliases, owned_aliases = resources["original_aliases"], resources["owned_aliases"]
    managed_aliases: dict[str, list[list[str]]] = {}
    managed_connections = {}
    for tab in ui["tabs"]:
        managed_connections[tab] = sorted(wiring_edges(ui, tab, definitions, endpoints))
        for receiver, sender in managed_connections[tab]:
            update.add_connection(from_item=receiver, to_item=sender, tab=tab)
        for receiver, sender in rebuilding["routes"].get(tab, ()):
            for receiver_id in endpoints.get(receiver, (receiver,)):
                for sender_id in endpoints.get(sender, (sender,)):
                    if (
                        receiver_id != sender_id
                        and (receiver_id, sender_id) not in managed_connections[tab]
                    ):
                        update.add_connection(from_item=receiver_id, to_item=sender_id, tab=tab)
        for group in original_aliases.get(tab, set()) - owned_aliases.get(tab, set()):
            update.add_alias(*sorted(group), tab=tab)
        managed_aliases[tab] = []
        for group in optional_ui("links/aliases.json").get(tab, []):
            fields = [
                value["parameter"]
                if "parameter" in value
                else datasets[value["dataset"]].fields.by_name(value["field"]).guid
                for value in group
            ]
            update.add_alias(*fields, tab=tab)
            managed_aliases[tab].append(fields)
        # Public apply_layout is a partial patch; unmanaged geometry, including
        # fractional coordinates already in remote metadata, remains verbatim.
        update.apply_layout(
            {key: tuple(value) for key, value in ui["layout"][tab].items()}, tab=tab
        )
    return managed_aliases, managed_connections


def configure_dashboard_settings(update: Any, settings: dict[str, Any], ui: dict[str, Any]) -> None:
    options = dict(settings.get("settings", {}))
    if "hide_tabs" in settings:
        options["hide_tabs"] = settings["hide_tabs"]
    cascading = any(set(recipients) & set(ui["selectors"]) for recipients in ui["wiring"].values())
    if cascading:
        if options.get("dependent_selectors") is False:
            msg = "Cascading selectors require dependent_selectors=True."
            raise DataLensConfigurationError(msg)
        options["dependent_selectors"] = True
    if options:
        update.settings(**options)
    for field in ("description", "access_description", "support_description"):
        if field in settings:
            getattr(update, field)(settings[field])
    if "global_parameters" in settings:
        update.global_params(
            {
                key: REMOVE_PARAM if value is None else value
                for key, value in settings["global_parameters"].items()
            }
        )


def verify_dashboard_draft(dashboard: Any, ui: dict[str, Any], definitions: dict[str, Any]) -> None:
    issues = persisted_layout_issues(dashboard)
    if issues:
        raise DataLensUtilsError("Dashboard geometry/metadata validation failed: " + str(issues))
    for tab in dashboard.tabs:
        if tab.id in ui["tabs"]:
            actual = {(edge["from"], edge["to"]) for edge in tab.connections}
            if not wiring_edges(
                ui, tab.id, definitions, endpoint_map(ui, (*tab.items, *tab.global_items))
            ).issubset(actual):
                msg = "Dashboard directed recipient metadata differs."
                raise DataLensUtilsError(msg)
            visible = {item.id for item in (*tab.items, *tab.global_items)}
            if not set(ui["layout"][tab.id]).issubset(visible):
                msg = "Dashboard shared selector occurrence is missing."
                raise DataLensUtilsError(msg)


def reconcile_dashboard(  # noqa: PLR0913 - Typed dashboard context and dependency snapshots.
    client: Any,
    store: Any,
    resource: Any,
    charts: dict[str, Any],
    datasets: dict[str, Any],
    definitions: dict[str, Any],
) -> Any:
    ui = load_ui(definitions)
    settings = resource.definition
    strict(settings.get("settings", {}), set(SETTINGS), "dashboard settings")
    name = current_session().deployment.dashboard_name
    dashboard = store.existing("dashboard", name, client.get.dashboard, scope="dash")
    if dashboard is None:
        builder = client.create.dashboard(name=name, location=store.folder)
        for key, tab in ui["tabs"].items():
            builder.add_tab(DashboardTab(tab["title"], tab_id=key, hidden=tab.get("hidden", False)))
        dashboard = store.create("dashboard", name, builder, client.get.dashboard, scope="dash")
    else:
        store.check_write("dashboard:main", dashboard)
    old = store.state["resources"]["dashboard"]

    if (
        old.get("fingerprint") == fingerprint(resource.files)
        and old.get("metadata", {}).get("published_id") == dashboard.published_id
        and not dashboard_issues(dashboard, settings, charts, datasets, definitions)
    ):
        return dashboard
    dashboard, rebuilding = prepare_rebuild(client, store, dashboard, ui, old)
    update = dashboard.update
    existing_tabs = {tab.id: tab for tab in dashboard.tabs}
    original_aliases = {
        tab: {frozenset(value) for value in groups} for tab, groups in rebuilding["aliases"].items()
    }
    owned_aliases = {
        tab: {frozenset(value) for value in values}
        for tab, values in old.get("managed_aliases", {}).items()
    }
    for tab in dashboard.tabs:
        for fields in owned_aliases.get(tab.id, set()) & actual_alias_groups(tab):
            update.remove_alias(*fields, tab=tab.id)
    for key, tab in ui["tabs"].items():
        if key in existing_tabs:
            update.update_tab(key, title=tab["title"], hidden=tab.get("hidden", False))
        else:
            update.add_tab(DashboardTab(tab["title"], tab_id=key, hidden=tab.get("hidden", False)))
    add_ui(update, ui, charts, datasets, definitions)
    endpoints = update_endpoints(ui, update)
    managed_aliases, managed_connections = wire_dashboard(
        update,
        ui,
        rebuilding,
        {
            "definitions": definitions,
            "datasets": datasets,
            "endpoints": endpoints,
            "original_aliases": original_aliases,
            "owned_aliases": owned_aliases,
        },
    )
    configure_dashboard_settings(update, settings, ui)
    update.reorder_tabs(
        [*ui["tabs"], *(tab.id for tab in dashboard.tabs if tab.id not in ui["tabs"])]
    )
    store.state["resources"]["dashboard"]["managed_items"] = sorted(
        set().union(*(set(value) for value in ui["layout"].values()))
    )
    store.state["resources"]["dashboard"]["managed_aliases"] = managed_aliases
    store.state["resources"]["dashboard"]["managed_connections"] = managed_connections
    store.save_checkpoint()
    dashboard = store.persisted(
        "dashboard", update.mode("save").execute(), client.get.dashboard, branch="saved"
    )
    verify_dashboard_draft(dashboard, ui, definitions)
    dashboard = store.persisted(
        "dashboard", dashboard.publish_revision(rev_id=dashboard.saved_id), client.get.dashboard
    )
    old.pop("dashboard_rebuild", None)
    return dashboard


def widget_issues(
    key: str, item: Any, expected: list[Any], ui: dict[str, Any], resources: dict[str, Any]
) -> list[str]:
    issues: list[str] = []
    charts, _datasets, definitions = (
        resources["charts"],
        resources["datasets"],
        resources["definitions"],
    )
    actual = item.data.get("tabs", ())
    if len(actual) != len(expected):
        issues.append("widgets." + key + ".members")
    default_index = next(
        (index for index, (_, wanted) in enumerate(expected) if wanted.get("default")),
        0,
    )
    for index, (value, (chart_key, wanted)) in enumerate(zip(actual, expected)):
        if (
            value.get("chartId") != charts[chart_key].id
            or value.get("title") != wanted.get("title", definitions[chart_key]["title"])
            or normalize_params(value.get("params", {}))
            != normalize_params(wanted.get("params", {}))
            or value.get("enableActionParams", False) != wanted.get("enable_action_params", False)
            or value.get("isDefault", False) != (index == default_index)
        ):
            issues.append("widgets." + key + ".binding_parameters_actions")
        for public, wire in (
            ("description", "description"),
            ("hint", "hint"),
            ("auto_height", "autoHeight"),
        ):
            if public in wanted and value.get(wire) != wanted[public]:
                issues.append("widgets." + key + "." + public)
    placement = ui["chart_groups"].get(key, ui["widgets"].get(key, definitions.get(key, {})))
    presentation = placement.get("presentation", {})
    for public, wire in (
        ("background", "background"),
        ("border_radius", "borderRadius"),
        ("pinned", "pinned"),
    ):
        if public in presentation and item.data.get(wire) != presentation[public]:
            issues.append("widgets." + key + "." + public)
    if "show_title" in presentation and item.data.get("hideTitle", False) != (
        not presentation["show_title"]
    ):
        issues.append("widgets." + key + ".show_title")
    return issues


def tab_widget_issues(tab: Any, ui: dict[str, Any], resources: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    _charts, _datasets, definitions = (
        resources["charts"],
        resources["datasets"],
        resources["definitions"],
    )
    tab_id = tab.id
    items = {item.id: item for item in (*tab.items, *tab.global_items)}
    for key, item in items.items():
        if key in ui["chart_groups"]:
            expected = [
                (member.get("chart", member["key"]), member)
                for member in ui["chart_groups"][key]["charts"]
            ]
        elif key in ui["widgets"]:
            widget = ui["widgets"][key]
            expected = [(widget["chart"], widget)]
        elif key in definitions and key not in ui["grouped_charts"]:
            expected = [
                (
                    key,
                    {
                        **definitions[key],
                        "params": optional_ui("links/chart_params.json").get(key, {}),
                    },
                )
            ]
        else:
            expected = []
        if expected:
            issues.extend(widget_issues(key, item, expected, ui, resources))
        for kind in ("titles", "texts"):
            wanted = optional_ui(kind + ".json").get(tab_id, {}).get(key)
            if wanted and item.data.get("text") != wanted["text"]:
                issues.append(kind + "." + key)
    return issues


def control_member_issues(
    member: Any, wanted: dict[str, Any], resources: dict[str, Any]
) -> list[str]:
    issues: list[str] = []
    charts, datasets, _definitions = (
        resources["charts"],
        resources["datasets"],
        resources["definitions"],
    )
    if wanted["source"]["kind"] == "editor":
        if member.source.chart_id != charts[wanted["source"]["chart"]].id:
            issues.append("selectors." + member.id + ".chart")
        return issues
    issues.extend(
        "selectors." + member.id + "." + value
        for value in _member_issues(member, wanted, datasets)
        if value != "unexpected selector influence scope"
    )
    affects = wanted.get("affects", "as_group")
    expected_type = (
        "allTabs"
        if affects == "all_tabs"
        else "selectedTabs"
        if isinstance(affects, list)
        else "asGroup"
    )
    if member.impact_type not in (None, expected_type) or (
        isinstance(affects, list) and set(member.raw.get("impactTabsIds", ())) != set(affects)
    ):
        issues.append("selectors." + member.id + ".affects")
    return issues


def tab_selector_issues(tab: Any, ui: dict[str, Any], resources: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    _charts, _datasets, _definitions = (
        resources["charts"],
        resources["datasets"],
        resources["definitions"],
    )
    tab_id = tab.id
    for wrapper, group in ui["wrappers"].items():
        occurrences = [item for item in (*tab.items, *tab.global_items) if item.id == wrapper]
        if tab_id in group["display"]:
            if len(occurrences) != 1:
                issues.append("selectors." + wrapper + ".occurrence")
                continue
            for control in tab.controls:
                if control.id != wrapper:
                    continue
                if [member.id for member in control.members] != group["members"]:
                    issues.append("selectors." + wrapper + ".members")
                for member in control.members:
                    issues.extend(
                        control_member_issues(member, ui["selectors"][member.id], resources)
                    )
        elif occurrences:
            issues.append("selectors." + wrapper + ".display_scope")
    return issues


def dashboard_settings_issues(
    dashboard: Any, settings: dict[str, Any], ui: dict[str, Any]
) -> list[str]:
    issues: list[str] = []
    for key, value in settings.get("settings", {}).items():
        actual = dashboard.data.get("settings", {})
        if SETTINGS[key] in actual if value is None else actual.get(SETTINGS[key]) != value:
            issues.append("settings." + key)
    cascading = any(set(recipients) & set(ui["selectors"]) for recipients in ui["wiring"].values())
    if cascading and not dashboard.data.get("settings", {}).get("dependentSelectors"):
        issues.append("settings.dependent_selectors")
    for key, value in settings.get("global_parameters", {}).items():
        actual = dashboard.data.get("settings", {}).get("globalParams", {})
        if (
            key in actual
            if value is None
            else normalize_params({key: actual.get(key)}) != normalize_params({key: value})
        ):
            issues.append("global_parameters." + key)
    if (
        "hide_tabs" in settings
        and dashboard.data.get("settings", {}).get("hideTabs", False) != settings["hide_tabs"]
    ):
        issues.append("settings.hide_tabs")
    for key, wire in (
        ("support_description", "supportDescription"),
        ("access_description", "accessDescription"),
    ):
        if key in settings and dashboard.data.get(wire, "") != settings[key]:
            issues.append(key)
    return issues


def dashboard_issues(
    dashboard: Any,
    settings: dict[str, Any],
    charts: dict[str, Any],
    datasets: dict[str, Any],
    definitions: dict[str, Any],
) -> list[str]:
    """Compare managed settings, placements, exact directed routes and selectors."""

    ui = load_ui(definitions)
    issues = []
    expected_tabs = list(ui["tabs"])
    if [tab.id for tab in dashboard.tabs if tab.id in ui["tabs"]] != expected_tabs:
        issues.append("tabs.order")
    actual_tabs = {tab.id: tab for tab in dashboard.tabs}
    for tab_id, definition in ui["tabs"].items():
        tab = actual_tabs.get(tab_id)
        if tab is None:
            issues.append("tabs." + tab_id + ".missing")
            continue
        if tab.title != definition["title"] or tab.hidden != definition.get("hidden", False):
            issues.append("tabs." + tab_id + ".presentation")
        items = {item.id: item for item in (*tab.items, *tab.global_items)}
        positions = {value.item_id: (value.x, value.y, value.w, value.h) for value in tab.layout}
        for key, value in ui["layout"][tab_id].items():
            if positions.get(key) != tuple(value) or key not in items:
                issues.append("layout." + tab_id + "." + key)
        endpoints = endpoint_map(ui, items.values())
        expected_edges = wiring_edges(ui, tab_id, definitions, endpoints)
        actual_edges = {(value["from"], value["to"]) for value in tab.connections}
        managed_ids = {
            identifier
            for reference, ids in endpoints.items()
            if reference in ui["wiring"]
            for identifier in ids
        }
        receivers = {identifier for ids in endpoints.values() for identifier in ids}
        managed_edges = {
            edge for edge in actual_edges if edge[1] in managed_ids and edge[0] in receivers
        }
        if expected_edges != managed_edges:
            issues.append("wiring." + tab_id)
        resources = {"charts": charts, "datasets": datasets, "definitions": definitions}
        issues.extend(tab_widget_issues(tab, ui, resources))
        issues.extend(tab_selector_issues(tab, ui, resources))
        expected_aliases = {
            frozenset(
                value["parameter"]
                if "parameter" in value
                else datasets[value["dataset"]].fields.by_name(value["field"]).guid
                for value in group
            )
            for group in optional_ui("links/aliases.json").get(tab_id, ())
        }
        if not expected_aliases.issubset(actual_alias_groups(tab)):
            issues.append("aliases." + tab_id)
    issues.extend(dashboard_settings_issues(dashboard, settings, ui))
    return issues
