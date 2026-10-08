"""Compare dashboard recipes with tolerant public SDK read views."""

from __future__ import annotations

import re
from typing import Any


def normalize_params(params: Any) -> Any:
    """SDK dashboard parameter values are lists of strings."""
    return {
        key: [
            str(value).lower() if isinstance(value, bool) else str(value)
            for value in (values if isinstance(values, (list, tuple)) else [values])
        ]
        for key, values in (params or {}).items()
    }


def definition_items(content: Any, charts: Any, chart_definitions: Any) -> Any:
    items = {}
    for kind in ("titles", "texts"):
        for key, value in content.get(kind, {}).items():
            items[key] = {"kind": kind[:-1], **value}
    for key, value in content.get("selectors", {}).items():
        if value["source"]["kind"] == "editor":
            items[key] = {
                "kind": "external_selector",
                **value,
                "chart": charts[value["source"]["chart"]],
            }
        else:
            items[key] = {
                "kind": "selector_group",
                "members": [value["key"]],
                "definitions": {value["key"]: value},
                "at": value["at"],
            }
    for key, value in content.get("selector_groups", {}).items():
        items[key] = {"kind": "selector_group", **value}
    for key, at in content.get("charts", {}).items():
        items[key] = {
            "kind": "chart",
            "chart": charts[key],
            "title": chart_definitions[key]["title"],
            "at": at,
            "params": content.get("chart_params", {}).get(key, {}),
        }
    for key, group in content.get("chart_groups", {}).items():
        items[key] = {
            "kind": "chart_group",
            **group,
            "tabs": [{**member, "chart": charts[member["key"]]} for member in group["charts"]],
        }
    return items


def managed_ids(items: Any) -> Any:
    return set(items) | {
        member for definition in items.values() for member in definition.get("members", [])
    }


def selector_default(value: Any) -> Any:
    """Use the datepicker's canonical UTC interval through the public string setter."""
    if isinstance(value, dict):

        def edge(text: Any, *, end: Any) -> Any:
            if text.startswith(("+", "-")):
                return "__relative_" + text
            if "T" not in text:
                return text + ("T23:59:59.999Z" if end else "T00:00:00.000Z")
            return (
                text if text.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", text) else text + "Z"
            )

        # SDK 3.1 DateInterval strips timestamps from DATE fields. A public str
        # default preserves the interval emitted by the actual datepicker UI:
        # https://github.com/datalens-tech/datalens-ui/blob/f581b7c31d6e9189ebeb1e1632b5fe7570534fb8/src/ui/libs/DatalensChartkit/components/Control/Items/Items.js#L383-L451
        return f"__interval_{edge(value['from'], end=False)}_{edge(value['to'], end=True)}"
    if isinstance(value, bool):
        return str(value).lower()
    return value


def _member_issues(member: Any, definition: Any, datasets: Any) -> Any:  # noqa: C901, PLR0912
    expected_source, control = (definition["source"], definition["control"])
    source, raw = (member.source, member.source.raw)
    issues = []
    if member.title != definition["title"] or member.source_type != expected_source["kind"]:
        issues.append("selector identity or title")
    if expected_source["kind"] == "dataset":
        dataset = datasets[expected_source["dataset"]]
        field = dataset.fields.by_name(expected_source["field"])
        if source.dataset_id != dataset.id or source.dataset_field_id != field.guid:
            issues.append("selector dataset field")
    elif source.param_name != expected_source["param_name"]:
        issues.append("selector parameter name")
    checks = (
        (source.element_type, control["element"]),
        (source.multiselect, control.get("multiselect", False)),
        (source.is_range, control.get("is_range", False)),
        (source.required, control.get("required", False)),
        (source.operation, control.get("operation")),
        (raw.get("showTitle", True), control.get("show_title", True)),
        (raw.get("titlePlacement", "left"), control.get("title_placement", "left")),
        (raw.get("innerTitle"), control.get("inner_title")),
        (raw.get("hint"), control.get("hint")),
    )
    if any((actual != expected for actual, expected in checks)):
        issues.append("selector control settings")
    actual_default = source.default_value
    expected_default = selector_default(control.get("default_value"))
    if control["element"] == "select":
        actual_default = [actual_default] if isinstance(actual_default, str) else actual_default
        expected_default = (
            [expected_default] if isinstance(expected_default, str) else expected_default
        )
    if actual_default != expected_default:
        issues.append("selector default")
    if expected_source["kind"] == "manual" and control["element"] == "select":
        options = []
        for option in control.get("options", []):
            if isinstance(option, str):
                options.append((option, option))
            elif isinstance(option, dict):
                options.append((option["value"], option["title"]))
            else:
                options.append(tuple(option))
        if [
            (option.get("value"), option.get("title")) for option in source.acceptable_values
        ] != options:
            issues.append("selector options")
    if member.impact_type not in (None, "asGroup"):
        issues.append("unexpected selector influence scope")
    return issues


def item_issues(tab: Any, item_id: Any, definition: Any, datasets: Any) -> Any:  # noqa: C901, PLR0912
    items = {item.id: item for item in (*tab.items, *tab.global_items)}
    positions = {item.item_id: (item.x, item.y, item.w, item.h) for item in tab.layout}
    item = items.get(item_id)
    if item is None:
        return ["item missing"]
    issues, kind = ([], definition["kind"])
    if kind == "selector_group":
        matching = [control for control in tab.controls if control.id == item_id]
        if len(matching) != 1 or item.item_type != "group_control":
            return ["selector wrapper missing or ambiguous"]
        control = matching[0]
        if [member.id for member in control.members] != definition["members"]:
            issues.append("selector members")
        for member in control.members:
            if member.id in definition["definitions"]:
                issues.extend(
                    f"{member.id}: {issue}"
                    for issue in _member_issues(
                        member, definition["definitions"][member.id], datasets
                    )
                )
        expected = {
            "buttonApply": definition.get("apply_button", False),
            "buttonReset": definition.get("reset_button", False),
            "updateControlsOnChange": definition.get("update_on_change", True),
            "showGroupName": definition.get("show_group_name", False),
            "autoHeight": definition.get("auto_height", False),
        }
        if (
            any((item.data.get(key, False) != value for key, value in expected.items()))
            or item.data.get("impactType") not in (None, "currentTab")
            or item.data.get("impactTabsIds", [tab.id]) != [tab.id]
        ):
            issues.append("selector group settings")
    elif kind == "external_selector":
        controls = [control for control in tab.controls if control.id == item_id]
        if item.item_type != "control" or len(controls) != 1 or len(controls[0].members) != 1:
            return ["external selector missing or ambiguous"]
        member = controls[0].members[0]
        if (
            member.source_type != "external"
            or member.source.chart_id != definition["chart"].id
            or member.title != definition["title"]
        ):
            issues.append("external selector settings")
    elif kind == "chart_group":
        actual = item.data.get("tabs", [])
        if item.item_type != "widget" or len(actual) != len(definition["tabs"]):
            return ["chart group tabs"]
        for widget, wanted in zip(actual, definition["tabs"]):
            if (
                widget.get("chartId") != wanted["chart"].id
                or widget.get("title") != wanted["title"]
                or widget.get("isDefault", False) != wanted.get("default", False)
                or (
                    normalize_params(widget.get("params", {}))
                    != normalize_params(wanted.get("params", {}))
                )
            ):
                issues.append("chart group reference, title, default or parameters")
    elif kind == "chart":
        widget_tabs = item.data.get("tabs", [])
        if item.item_type != "widget" or len(widget_tabs) != 1:
            return ["chart widget settings"]
        widget = widget_tabs[0]
        if (
            widget.get("chartId") != definition["chart"].id
            or widget.get("title") != definition["title"]
        ):
            issues.append("chart widget reference or title")
        if normalize_params(widget.get("params", {})) != normalize_params(
            definition.get("params", {})
        ):
            issues.append("chart widget parameters")
    elif item.item_type != kind or item.data.get("text") != definition["text"]:
        issues.append("text or title")
    if kind == "title" and item.data.get("size") != definition.get("size", "m"):
        issues.append("title size")
    if positions.get(item_id) != tuple(definition["at"]):
        issues.append("placement")
    return issues


def selector_bindings(content: Any) -> Any:
    """Map member/external selector IDs to their declared receiver widget IDs."""
    definitions = {value["key"]: value for value in content.get("selectors", {}).values()}
    for group in content.get("selector_groups", {}).values():
        definitions.update(group["definitions"])
    return {key: set(definition["recipients"]) for key, definition in definitions.items()}


def expected_edges(content: Any) -> Any:
    widgets = set(content.get("charts", {})) | set(content.get("chart_groups", {}))
    bindings = selector_bindings(content)
    receivers = widgets | set(bindings)
    return {
        (receiver, selector)
        for selector, recipients in bindings.items()
        for receiver in receivers - recipients - {selector}
    }


def normalized_edges(tab: Any, *, all_routes: bool = False) -> Any:
    """Return logical endpoints and retain wire IDs needed by remove_connection."""
    logical = {}
    for item in (*tab.items, *tab.global_items):
        logical[item.id] = item.id
        for chart_tab in item.data.get("tabs", []) if item.item_type == "widget" else []:
            logical[chart_tab.get("id")] = item.id
    for control in tab.controls:
        for member in control.members:
            logical[member.id] = member.id
    edges: dict[Any, Any] = {}
    for edge in tab.connections:
        source, target = (edge.get("from"), edge.get("to"))
        key = (logical.get(source, source), logical.get(target, target))
        edges.setdefault(key, []).append((source, target))
    return edges if all_routes else {key: routes[-1] for key, routes in edges.items()}


def alias_groups(content: Any, datasets: Any) -> Any:
    return {
        frozenset(
            field["parameter"]
            if "parameter" in field
            else datasets[field["dataset"]].fields.by_name(field["field"]).guid
            for field in group
        )
        for group in content.get("aliases", [])
    }


def actual_alias_groups(tab: Any) -> Any:
    return {
        frozenset(group)
        for group in tab.aliases.get("default", [])
        if isinstance(group, (list, tuple))
    }


def managed_edges(tab: Any, content: Any, *, all_routes: bool = False) -> Any:
    selectors = set(selector_bindings(content))
    receivers = set(content.get("charts", {})) | set(content.get("chart_groups", {})) | selectors
    return {
        edge: wire
        for edge, wire in normalized_edges(tab, all_routes=all_routes).items()
        if edge[0] in receivers and edge[1] in selectors
    }


def requires_dependent_selectors(contents: Any) -> bool:
    """Cascading values are needed when a selector receives another selector."""
    return any(
        bool(set(recipients) & set(bindings))
        for content in contents.values()
        for bindings in [selector_bindings(content)]
        for recipients in bindings.values()
    )


def dashboard_issues(  # noqa: C901, PLR0913
    dashboard: Any,
    *,
    tab_definitions: Any,
    contents: Any,
    datasets: Any,
    charts: Any,
    chart_definitions: Any,
    description: Any,
    hide_tabs: Any,
) -> Any:
    issues = [
        issue
        for issue in dashboard.validate()
        if not (
            issue.kind in ("overlap", "layout_reflow")
            and tab_definitions.get(issue.tab_id, {}).get("preserve_layout", False)
        )
    ]
    tabs = {tab.id: tab for tab in dashboard.tabs}
    unexpected = [
        tab.id for tab in dashboard.tabs if not tab.hidden and tab.id not in tab_definitions
    ]
    if unexpected:
        issues.append(f"unexpected visible tabs: {unexpected}")
    if dashboard.data.get("settings", {}).get("hideTabs", False) != hide_tabs:
        issues.append("dashboard tab visibility")
    if requires_dependent_selectors(contents) and not dashboard.data.get("settings", {}).get(
        "dependentSelectors", False
    ):
        issues.append("dependent selectors disabled")
    if (dashboard.raw.get("annotation") or {}).get("description", "") != description:
        issues.append("dashboard description")
    for role, definition in tab_definitions.items():
        tab = tabs.get(role)
        if tab is None:
            issues.append(f"missing tab {role}")
            continue
        if tab.title != definition["title"] or tab.hidden != definition["hidden"]:
            issues.append(f"tab settings {role}")
        content = contents.get(role, {})
        for item_id, item in definition_items(content, charts, chart_definitions).items():
            issues.extend(
                f"{role}/{item_id}: {issue}" for issue in item_issues(tab, item_id, item, datasets)
            )
        if set(managed_edges(tab, content)) != expected_edges(content):
            issues.append(f"selector receiver bindings {role}")
        if not alias_groups(content, datasets).issubset(actual_alias_groups(tab)):
            issues.append(f"dataset field aliases {role}")
    return issues
