"""Decode selected tabs into stable placements, controls and directed routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from .bi_dashboard import SETTINGS
from .errors import DataLensUtilsError


def check_widget_member(value: Any, identifier: str) -> None:
    allowed = {
        "id",
        "chartId",
        "title",
        "isDefault",
        "params",
        "enableActionParams",
        "description",
        "hint",
        "autoHeight",
        "enableDescription",
        "enableHint",
    }
    unsupported = set(value) - allowed
    for text, enabled in (("description", "enableDescription"), ("hint", "enableHint")):
        if value.get(enabled, False) != bool(value.get(text)):
            unsupported.add(enabled)
    if unsupported:
        raise DataLensUtilsError(
            "Chart widget member properties cannot be faithfully written: "
            + identifier
            + ": "
            + ", ".join(sorted(unsupported))
        )


def item_source_issues(item: Any, *, allow_defaults: bool = False) -> set[str]:
    if not item.id:
        message = "Dashboard item identity cannot be faithfully written."
        raise DataLensUtilsError(message)
    unsupported = set(item.raw) - {"id", "type", "namespace", "data", "defaults"}
    if not allow_defaults and item.raw.get("defaults") not in (None, {}):
        unsupported.add("defaults")
    if item.raw.get("namespace") not in (None, "default"):
        unsupported.add("namespace")
    return unsupported


def check_static_source(item: Any) -> None:
    unsupported = item_source_issues(item)
    allowed = {"text", "size"} if item.item_type == "title" else {"text"}
    neutral: dict[str, Any] = {"autoHeight": True, "pinned": False}
    if item.item_type == "title":
        neutral["showInTOC"] = False
    else:
        # Neutral default emitted by the supported SDK 3.1/3.2 text builders.
        neutral["backgroundSettings"] = {"color": {"light": "#FFFFFF", "dark": "#343535"}}
    unsupported.update(
        "data." + key
        for key, value in item.data.items()
        if key not in allowed and (key not in neutral or value != neutral[key])
    )
    if unsupported:
        raise DataLensUtilsError(
            "Text/title properties cannot be faithfully written: "
            + item.id
            + ": "
            + ", ".join(sorted(unsupported))
        )


def check_widget_source(item: Any) -> None:
    unsupported = item_source_issues(item)
    unsupported.update(
        "data." + name for name in set(item.data) - {"tabs", "hideTitle", "borderRadius", "pinned"}
    )
    if unsupported:
        raise DataLensUtilsError(
            "Chart widget properties cannot be faithfully written: "
            + item.id
            + ": "
            + ", ".join(sorted(unsupported))
        )
    for value in item.data.get("tabs", ()):
        check_widget_member(value, item.id)


def import_widget(item: Any, tab: Any, endpoints: dict[str, str], context: Any) -> None:
    check_widget_source(item)
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
            if wire in value and value[wire] != "":
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
        message = "Widget has no chart bindings: " + item.id
        raise DataLensUtilsError(message)
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
            message = "Unsupported selector source: " + str(member.source_type)
            raise DataLensUtilsError(message)
        control_options(member, definition, selected)
    files["configs/UI/selectors/" + member.id + ".json"] = definition
    endpoints[member.id] = member.id
    senders.add(member.id)


def check_control_source(control: Any) -> None:
    if control.item.data.get("pinned"):
        raise DataLensUtilsError(
            "Selector pinning cannot be faithfully written through the public SDK: " + control.id
        )
    if any(member.source_type == "external" for member in control.members):
        data = control.item.data
        unsupported = item_source_issues(control.item)
        unsupported.update(set(data) - {"title", "sourceType", "source", "pinned"})
        if data.get("title") is not None and not isinstance(data["title"], str):
            unsupported.add("title")
        if unsupported or control.item.defaults or set(data.get("source", {})) - {"chartId"}:
            raise DataLensUtilsError(
                "Editor selector wrapper settings cannot be faithfully written: " + control.id
            )
        return
    check_native_control(control)


def check_selector_defaults(member: Any) -> None:
    source = member.source
    value = source.default_value
    if value is None:
        value = [] if source.element_type == "select" else ""
    elif source.operation:
        prefix = "__" + source.operation.lower() + "_"
        if isinstance(value, list):
            if any(not isinstance(item, str) for item in value):
                raise DataLensUtilsError(
                    "Selector defaults cannot be faithfully written: " + member.id
                )
            value = [prefix + item for item in value]
        else:
            value = prefix + str(value)
    key = source.dataset_field_id or source.param_name
    if member.defaults != {key: value} and (member.defaults or source.default_value is not None):
        raise DataLensUtilsError("Selector defaults cannot be faithfully written: " + member.id)


def selector_source_issues(member: Any) -> set[str]:
    source = member.source.raw
    source_keys = {
        "datasetId",
        "datasetFieldId",
        "datasetFieldType",
        "fieldType",
        "fieldName",
        "elementType",
        "showTitle",
        "titlePlacement",
        "innerTitle",
        "hint",
        "showHint",
        "defaultValue",
        "multiselectable",
        "isRange",
        "required",
        "operation",
        "acceptableValues",
    }
    unsupported = {"source." + name for name in set(source) - source_keys}
    for name in ("showTitle", "showHint", "multiselectable", "isRange", "required"):
        if name in source and not isinstance(source[name], bool):
            unsupported.add("source." + name)
    for name in (
        "datasetId",
        "datasetFieldId",
        "datasetFieldType",
        "fieldType",
        "fieldName",
        "elementType",
        "titlePlacement",
        "innerTitle",
        "hint",
        "operation",
    ):
        if name in source and source[name] is not None and not isinstance(source[name], str):
            unsupported.add("source." + name)
    if source.get("showHint", False) != bool(source.get("hint")):
        unsupported.add("source.showHint")
    if any(set(option) - {"title", "value"} for option in member.source.acceptable_values):
        unsupported.add("source.acceptableValues")
    options = source.get("acceptableValues", [])
    if not isinstance(options, (list, tuple)) or len(options) != len(
        member.source.acceptable_values
    ):
        unsupported.add("source.acceptableValues")
    if options and (member.source_type != "manual" or member.source.element_type != "select"):
        unsupported.add("source.acceptableValues")
    return unsupported


def check_selector_member(member: Any) -> None:
    if not member.id:
        message = "Selector member identity cannot be faithfully written."
        raise DataLensUtilsError(message)
    raw = member.raw
    # Standalone controls keep wrapper metadata in raw; group members keep it directly.
    if "data" in raw:
        raw = raw["data"]
    allowed = {
        "id",
        "title",
        "namespace",
        "sourceType",
        "placementMode",
        "width",
        "source",
        "defaults",
        "impactType",
        "impactTabsIds",
        "pinned",
    }
    unsupported = set(raw) - allowed
    if raw.get("title") is not None and not isinstance(raw["title"], str):
        unsupported.add("title")
    for name, default in (("namespace", "default"), ("placementMode", "auto"), ("width", "")):
        if raw.get(name, default) != default:
            unsupported.add(name)
    unsupported.update(selector_source_issues(member))
    if unsupported:
        raise DataLensUtilsError(
            "Selector member properties cannot be faithfully written: "
            + member.id
            + ": "
            + ", ".join(sorted(unsupported))
        )
    check_selector_defaults(member)


def check_native_control(control: Any) -> None:
    standalone = control.wire_format == "standalone_control"
    unsupported = item_source_issues(control.item, allow_defaults=standalone)
    allowed = {
        "group",
        "autoHeight",
        "buttonApply",
        "buttonReset",
        "updateControlsOnChange",
        "showGroupName",
        "impactType",
        "impactTabsIds",
        "borderRadius",
        "pinned",
    }
    if standalone:
        allowed = {"title", "sourceType", "source", "pinned", "impactType", "impactTabsIds"}
    unsupported.update(set(control.item.data) - allowed)
    members = control.item.data.get("group", [])
    if not standalone and (
        not isinstance(members, (list, tuple))
        or not members
        or len(members) != len(control.members)
    ):
        unsupported.add("group")
    if unsupported:
        raise DataLensUtilsError(
            "Selector wrapper properties cannot be faithfully written: "
            + control.id
            + ": "
            + ", ".join(sorted(unsupported))
        )
    for member in control.members:
        check_selector_member(member)


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
        check_control_source(control)
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
            group["show_on_tabs"] = (
                "all" if control.item.data.get("impactType") == "allTabs" else shared
            )
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
            check_static_source(item)
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
        if wire in source.raw and source.raw[wire] != "":
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
                message = "Unresolved directed wiring endpoint on tab " + tab.id
                raise DataLensUtilsError(message)
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
