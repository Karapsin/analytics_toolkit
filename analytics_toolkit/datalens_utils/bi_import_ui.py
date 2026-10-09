"""Decode selected tabs into stable placements, controls and directed routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from .bi_dashboard import SETTINGS
from .errors import DataLensUtilsError


def import_widget(item: Any, tab: Any, endpoints: dict[str, str], context: Any) -> None:
    data = item.data
    files, chart_keys, member_keys, senders = (
        context.files,
        context.chart_keys,
        context.member_keys,
        context.senders,
    )
    members = []
    for index, value in enumerate(data.get("tabs", ())):
        reference = chart_keys[value["chartId"]]
        member = {
            "key": member_keys[item.id][index]
            if member_keys and item.id in member_keys
            else value["id"],
            "chart": reference,
            "title": value.get("title", ""),
            "default": value.get("isDefault", False),
            "params": value.get("params", {}),
            "enable_action_params": value.get("enableActionParams", False),
        }
        for public, wire in (
            ("description", "description"),
            ("hint", "hint"),
            ("auto_height", "autoHeight"),
        ):
            if wire in value:
                member[public] = value[wire]
        members.append(member)
    presentation = {
        public: data[wire]
        for public, wire in (
            ("background", "background"),
            ("border_radius", "borderRadius"),
            ("pinned", "pinned"),
        )
        if wire in data
    }
    if "hideTitle" in data:
        presentation["show_title"] = not data["hideTitle"]
    if len(members) == 1:
        member = members[0]
        widget = {
            "chart": member["chart"],
            "tab": tab.id,
            "title": member["title"],
            "params": member["params"],
            "enable_action_params": member["enable_action_params"],
            "presentation": presentation,
        }
        for name in ("description", "hint", "auto_height"):
            if name in member:
                widget["presentation"][name] = member[name]
        files["configs/UI/widgets.json"][item.id] = widget
        endpoints[data["tabs"][0]["id"]] = item.id
    elif members:
        files["configs/UI/chart_groups.json"][item.id] = {
            "tab": tab.id,
            "charts": members,
            "presentation": presentation,
        }
        endpoints.update(
            {
                value["id"]: item.id + "/" + member["key"]
                for value, member in zip(data["tabs"], members)
            }
        )
    else:
        raise DataLensUtilsError("Widget has no chart bindings: " + item.id)
    senders.update(
        endpoints[value["id"]]
        for value, member in zip(data["tabs"], members)
        if member["enable_action_params"]
    )


def import_control_member(
    member: Any, control_context: Any, endpoints: dict[str, str], context: Any
) -> None:
    wrapper, tab, shared, external = (
        control_context.wrapper,
        control_context.tab,
        control_context.shared,
        control_context.external,
    )
    files, chart_keys, dataset_keys, selected, senders = (
        context.files,
        context.chart_keys,
        context.dataset_keys,
        context.selected,
        context.senders,
    )
    source = member.source
    definition = {"key": member.id, "tab": tab.id, "title": member.title or ""}
    if member.source_type == "external":
        if not external or len(shared) != 1:
            msg = "External selector group/display cannot be represented."
            raise DataLensUtilsError(msg)
        definition["source"] = {"kind": "editor", "chart": chart_keys[source.chart_id]}
    else:
        definition["group"] = wrapper
        if member.source_type == "dataset":
            definition["source"] = {
                "kind": "dataset",
                "dataset": dataset_keys[source.dataset_id],
                "field": context.source_fields[(source.dataset_id, source.dataset_field_id)],
            }
        elif member.source_type == "manual":
            definition["source"] = {"kind": "manual", "param_name": source.param_name}
        else:
            raise DataLensUtilsError("Unsupported selector source: " + str(member.source_type))
        control_options(member, definition, selected)
    files["configs/UI/selectors/" + member.id + ".json"] = definition
    endpoints[member.id] = member.id
    senders.add(member.id)


def import_controls(tab: Any, endpoints: dict[str, str], context: Any) -> None:
    files, managed_items, occurrences, selected, senders = (
        context.files,
        context.managed_items,
        context.occurrences,
        context.selected,
        context.senders,
    )
    for control in tab.controls:
        wrapper = control.id
        if managed_items is not None and wrapper not in managed_items:
            continue
        shared = occurrences[wrapper]
        if shared[0] != tab.id:
            endpoints.update({member.id: member.id for member in control.members})
            senders.update(member.id for member in control.members)
            continue
        group = {"tab": tab.id, "members": [member.id for member in control.members]}
        if wrapper in {value.id for value in tab.global_items}:
            actual_scope = control.item.data.get("impactTabsIds")
            if actual_scope and set(actual_scope) - selected:
                msg = "Shared selector reaches unselected tabs; import the whole dashboard."
                raise DataLensUtilsError(msg)
            group["show_on_tabs"] = shared
        for public, wire in (
            ("apply_button", "buttonApply"),
            ("reset_button", "buttonReset"),
            ("update_on_change", "updateControlsOnChange"),
            ("show_group_name", "showGroupName"),
            ("auto_height", "autoHeight"),
            ("border_radius", "borderRadius"),
        ):
            if wire in control.item.data:
                group[public] = control.item.data[wire]
        external = len(control.members) == 1 and control.members[0].source_type == "external"
        if not external:
            files["configs/UI/selectors/selector_groups.json"][wrapper] = group
        control_context = SimpleNamespace(
            wrapper=wrapper, tab=tab, shared=shared, external=external
        )
        for member in control.members:
            import_control_member(member, control_context, endpoints, context)


def check_tab_routes(
    endpoints: dict[str, str],
    senders: set[str],
    ignored: set[tuple[str, str]],
    routing: dict[tuple[str, str], bool],
) -> None:
    for receiver in endpoints.values():
        for sender in set(endpoints.values()) & senders:
            if receiver == sender:
                continue
            pair = receiver, sender
            if pair in routing and routing[pair] != (pair in ignored):
                message = (
                    "Per-tab recipient routes differ for "
                    + str(pair)
                    + "; recipe import cannot preserve them."
                )
                raise DataLensUtilsError(message)
            routing[pair] = pair in ignored


def index_fields(
    datasets: dict[str, Any],
) -> tuple[dict[str, Any], set[str], dict[tuple[str, str], str]]:
    fields: dict[str, Any] = {}
    ambiguous: set[str] = set()
    for key, dataset in datasets.items():
        for field in dataset.fields:
            if field.guid in fields:
                ambiguous.add(field.guid)
            fields[field.guid] = {"dataset": key, "field": field.title}
    source_fields = {
        (dataset.id, field.guid): field.title
        for dataset in datasets.values()
        for field in dataset.fields
    }
    return fields, ambiguous, source_fields


def import_tab_items(tab: Any, endpoints: dict[str, str], context: Any) -> None:
    managed_items, files = context.managed_items, context.files
    for item in (*tab.items, *tab.global_items):
        if managed_items is not None and item.id not in managed_items:
            continue
        data = item.data
        if item.item_type == "widget":
            import_widget(item, tab, endpoints, context)
        elif item.item_type in {"title", "text"}:
            path = "configs/UI/" + ("titles" if item.item_type == "title" else "texts") + ".json"
            value = {"text": data.get("text", "")}
            if item.item_type == "title":
                value["size"] = data.get("size", "m")
            files[path].setdefault(tab.id, {})[item.id] = value
        elif item.item_type not in {"control", "group_control"}:
            raise DataLensUtilsError(
                "Unsupported dashboard item: " + item.item_type + " (" + item.id + ")"
            )


def control_options(member: Any, definition: dict[str, Any], selected: set[str]) -> None:
    source = member.source
    definition["control"] = {
        "element": source.element_type,
        "default_value": source.default_value,
        "multiselect": source.multiselect,
        "is_range": source.is_range,
        "required": source.required,
    }
    if source.operation is not None:
        definition["control"]["operation"] = source.operation
    if member.source_type == "manual" and source.element_type == "select":
        definition["control"]["options"] = list(source.acceptable_values)
    for public, wire in (
        ("show_title", "showTitle"),
        ("title_placement", "titlePlacement"),
        ("inner_title", "innerTitle"),
        ("hint", "hint"),
    ):
        if wire in source.raw:
            definition["control"][public] = source.raw[wire]
    if member.impact_type == "allTabs":
        definition["affects"] = "all_tabs"
    elif member.impact_type == "selectedTabs":
        if set(member.impact_tabs_ids or ()) - selected:
            msg = "Selector influence reaches unselected tabs; import the whole dashboard."
            raise DataLensUtilsError(msg)
        definition["affects"] = list(member.impact_tabs_ids or ())


def active_senders(tab: Any, endpoints: dict[str, str], senders: set[str]) -> set[str]:
    inactive = {
        member.id
        for control in tab.controls
        for member in control.members
        if member.impact_type == "selectedTabs" and tab.id not in (member.impact_tabs_ids or ())
    }
    return (senders & set(endpoints.values())) - inactive


def ui_definition(  # noqa: PLR0913 - Stable import identities and managed projection options.
    dashboard: Any,
    selected: set[str],
    chart_keys: dict[str, str],
    dataset_keys: dict[str, str],
    datasets: dict[str, Any],
    *,
    managed_items: set[str] | None = None,
    member_keys: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    files: dict[str, Any] = {
        name: {}
        for name in (
            "configs/UI/tabs.json",
            "configs/UI/layout.json",
            "configs/UI/widgets.json",
            "configs/UI/chart_groups.json",
            "configs/UI/selectors/selector_groups.json",
            "configs/UI/titles.json",
            "configs/UI/texts.json",
            "configs/UI/links/connections.json",
            "configs/UI/links/aliases.json",
            "configs/UI/links/chart_params.json",
        )
    }
    tabs = [tab for tab in dashboard.tabs if tab.id in selected]
    occurrences: dict[str, list[str]] = {}
    for tab in tabs:
        for item in (*tab.items, *tab.global_items):
            if managed_items is not None and item.id not in managed_items:
                continue
            occurrences.setdefault(item.id, []).append(tab.id)
    fields, ambiguous, source_fields = index_fields(datasets)
    all_receivers: set[str] = set()
    ignored: set[tuple[str, str]] = set()
    routing: dict[tuple[str, str], bool] = {}
    senders: set[str] = set()
    context = SimpleNamespace(
        files=files,
        chart_keys=chart_keys,
        member_keys=member_keys,
        senders=senders,
        dataset_keys=dataset_keys,
        fields=fields,
        selected=selected,
        managed_items=managed_items,
        occurrences=occurrences,
        source_fields=source_fields,
    )
    for tab in tabs:
        files["configs/UI/tabs.json"][tab.id] = {"title": tab.title, "hidden": tab.hidden}
        files["configs/UI/layout.json"][tab.id] = {
            value.item_id: [
                int(number) if float(number).is_integer() else number
                for number in (value.x, value.y, value.w, value.h)
            ]
            for value in tab.layout
        }
        endpoints: dict[str, str] = {}
        import_tab_items(tab, endpoints, context)
        import_controls(tab, endpoints, context)
        all_receivers.update(endpoints.values())
        tab_ignored: set[tuple[str, str]] = set()
        for edge in tab.connections:
            if edge.get("from") not in endpoints or edge.get("to") not in endpoints:
                if managed_items is not None:
                    continue
                raise DataLensUtilsError("Unresolved directed wiring endpoint on tab " + tab.id)
            tab_ignored.add((endpoints[edge["from"]], endpoints[edge["to"]]))
        check_tab_routes(endpoints, active_senders(tab, endpoints, senders), tab_ignored, routing)
        ignored.update(tab_ignored)
        alias_groups = tab.aliases.get("default", ())
        if any(value in ambiguous for group in alias_groups for value in group):
            message = "Alias field GUID belongs to multiple datasets; import cannot infer identity."
            raise DataLensUtilsError(message)
        files["configs/UI/links/aliases.json"][tab.id] = [
            [fields.get(value, {"parameter": value}) for value in group] for group in alias_groups
        ]
    files["configs/UI/links/connections.json"] = {
        sender: sorted(
            receiver
            for receiver in all_receivers
            if receiver != sender and (receiver, sender) not in ignored
        )
        for sender in sorted(senders)
    }
    settings = dashboard.data.get("settings", {})
    files["imported_dashboard_settings"] = {
        public: settings[wire] for public, wire in SETTINGS.items() if wire in settings
    }
    files["imported_global_parameters"] = settings.get("globalParams", {})
    return files
