"""Resolve readable, recursively organized UI configuration before remote writes."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session as session
from analytics_toolkit.datalens_utils.settings import read_chart_definitions, read_config


def read_selector_definitions() -> Any:
    """Read one selector per file; identity belongs to its key, not its path."""
    definitions = {}
    for path in sorted((session().paths.project_root / "configs/UI/selectors").rglob("*.json")):
        if path.name == "selector_groups.json":
            continue
        definition = json.loads(path.read_text(encoding="utf-8"))
        key = definition.get("key")
        if not isinstance(key, str) or not key:
            message = (
                "Selector definition needs a nonempty key: "
                f"{path.relative_to(session().paths.project_root)}"
            )
            raise DataLensUtilsError(message)
        if key in definitions:
            message = f"Duplicate selector key: {key}"
            raise DataLensUtilsError(message)
        definitions[key] = definition
    return definitions


def _validate_positions(tab: Any, positions: Any, *, allow_overlaps: Any = False) -> Any:
    occupied: list[Any] = []
    for key, at in positions.items():
        if not isinstance(at, list) or len(at) != 4 or any(type(value) is not int for value in at):  # noqa: PLR2004
            message = f"UI position {tab}/{key} must be [x, y, w, h] integers."
            raise DataLensUtilsError(message)
        x, y, w, h = at
        if x < 0 or y < 0 or w < 1 or h < 1 or x + w > 36:  # noqa: PLR2004
            message = f"UI position {tab}/{key} is outside the 36-column grid."
            raise DataLensUtilsError(message)
        for other, (ox, oy, ow, oh) in occupied:
            if not allow_overlaps and x < ox + ow and ox < x + w and y < oy + oh and oy < y + h:
                message = f"UI items overlap in {tab}: {other}, {key}"
                raise DataLensUtilsError(message)
        occupied.append((key, at))


_MIN_ALIAS_MEMBERS = 2


def _declared_fields(dataset: Any) -> Any:
    return (
        {value.get("title", key) for key, value in dataset.get("fields", {}).items()}
        | set(dataset.get("calculations", {}))
        | set(dataset.get("parameters", {}))
    )


def _parameter_names(chart: Any, datasets: Any) -> Any:
    if chart["family"] == "wizard":
        return set(datasets[chart["dataset"]].get("parameters", {}))
    return {value["name"] for value in chart.get("params", [])}


def _validate_selector(  # noqa: C901, PLR0913, PLR0912, PLR0915
    key: Any,
    definition: Any,
    *,
    tabs: Any,
    charts: Any,
    datasets: Any,
    selectors: Any = None,
    chart_groups: Any = None,
) -> Any:
    selectors = {} if selectors is None else selectors
    tab = definition.get("tab")
    if tab not in tabs:
        message = f"Selector {key} refers to unknown tab {tab!r}."
        raise DataLensUtilsError(message)
    source = definition.get("source", {})
    kind = source.get("kind")
    if kind == "dataset":
        dataset = datasets.get(source.get("dataset"))
        if dataset is None or source.get("field") not in _declared_fields(dataset):
            message = f"Selector {key} refers to an unknown dataset field."
            raise DataLensUtilsError(message)
    elif kind == "manual":
        if not source.get("param_name"):
            message = f"Manual selector {key} needs param_name."
            raise DataLensUtilsError(message)
    elif kind == "editor":
        if source.get("chart") not in charts or charts[source["chart"]].get("type") != "selector":
            message = f"External selector {key} needs an Editor selector object."
            raise DataLensUtilsError(message)
        if definition.get("group") or definition.get("control"):
            message = f"External selector {key} cannot have native controls or groups."
            raise DataLensUtilsError(message)
    else:
        message = f"Unknown selector source kind for {key}: {kind!r}"
        raise DataLensUtilsError(message)
    recipients = definition.get("recipients")
    if not isinstance(recipients, list) or len(recipients) != len(set(recipients)):
        message = f"Selector {key} needs a unique recipients list."
        raise DataLensUtilsError(message)
    for recipient in recipients:
        group = (chart_groups or {}).get(recipient)
        if group is not None:
            if group["tab"] != tab:
                message = f"Selector {key} has a cross-tab chart group: {recipient}"
                raise DataLensUtilsError(message)
            if kind == "manual":
                for chart_tab in group["charts"]:
                    chart = charts[chart_tab["key"]]
                    if source["param_name"] not in _parameter_names(chart, datasets):
                        message = (
                            f"Parameter {source['param_name']} is not declared by "
                            f"{chart_tab['key']}."
                        )
                        raise DataLensUtilsError(message)
            continue
        receiver = charts.get(recipient)
        if receiver is None and kind in ("manual", "dataset"):
            receiver = selectors.get(recipient)
            if (
                receiver is not None
                and receiver["source"]["kind"] == "dataset"
                and (receiver["tab"] == tab)
            ):
                if kind == "manual" and source["param_name"] not in datasets[
                    receiver["source"]["dataset"]
                ].get("parameters", {}):
                    message = f"Selector {key} parameter is not declared by {recipient}."
                    raise DataLensUtilsError(message)
                continue
            receiver = None
        if (
            receiver is None
            or receiver.get("type") == "selector"
            or receiver.get("tab", receiver.get("family")) != tab
        ):
            message = f"Selector {key} has an unknown or cross-tab recipient: {recipient}"
            raise DataLensUtilsError(message)
        if kind == "manual":
            names = _parameter_names(receiver, datasets)
            if source["param_name"] not in names:
                message = (
                    f"Selector {key} parameter {source['param_name']!r} is not declared by "
                    f"{recipient}."
                )
                raise DataLensUtilsError(message)
    if kind != "editor":
        control = definition.get("control", {})
        if control.get("element") not in {"select", "date", "input", "checkbox"}:
            message = f"Selector {key} has an unsupported native control element."
            raise DataLensUtilsError(message)
        if kind == "manual" and "operation" in control:
            message = (
                f"Manual parameter selector {key} cannot apply a dataset comparison operation."
            )
            raise DataLensUtilsError(message)
        if control.get("is_range") and control.get("element") != "date":
            message = f"Selector {key} range is supported only for date controls."
            raise DataLensUtilsError(message)
        allowed = {
            "element",
            "multiselect",
            "is_range",
            "default_value",
            "options",
            "operation",
            "required",
            "show_title",
            "title_placement",
            "inner_title",
            "hint",
        }
        if set(control) - allowed:
            message = (
                f"Selector {key} has unsupported control settings: {sorted(set(control) - allowed)}"
            )
            raise DataLensUtilsError(message)
        default = control.get("default_value")
        if control["element"] == "checkbox" and type(default) is not bool:
            message = f"Checkbox selector {key} requires a boolean default."
            raise DataLensUtilsError(message)
        if control.get("title_placement", "left") not in {"left", "top"}:
            message = f"Selector {key} has an unsupported title placement."
            raise DataLensUtilsError(message)
        if isinstance(default, dict) and (
            set(default) != {"from", "to"} or not control.get("is_range")
        ):
            message = f"Selector {key} has an invalid date interval default."
            raise DataLensUtilsError(message)


def read_contents(*, allow_overlaps: Any = False) -> Any:  # noqa: C901, PLR0912, PLR0915
    """Combine separate UI files into validated tab-level content definitions."""
    layouts = read_config("UI/layout.json")
    charts = read_chart_definitions()
    datasets = read_config("DL objects/datasets.json")
    tabs = read_config("UI/tabs.json")
    titles, texts = (read_config(f"UI/{kind}.json") for kind in ("titles", "texts"))
    selectors = read_selector_definitions()
    groups = read_config("UI/selectors/selector_groups.json")
    aliases = read_config("UI/links/aliases.json")
    chart_params = read_config("UI/links/chart_params.json")
    chart_groups = (
        read_config("UI/chart_groups.json")
        if (session().paths.project_root / "configs/UI/chart_groups.json").exists()
        else {}
    )
    grouped_charts = {
        member["key"] for group in chart_groups.values() for member in group["charts"]
    }
    if sum(len(group["charts"]) for group in chart_groups.values()) != len(grouped_charts):
        message = "A chart can belong to only one chart group."
        raise DataLensUtilsError(message)
    for key, group in chart_groups.items():
        if (
            group["tab"] not in tabs
            or not group["charts"]
            or sum(member.get("default", False) for member in group["charts"]) != 1
        ):
            message = f"Chart group {key} needs a known tab and one default chart."
            raise DataLensUtilsError(message)
        for member in group["charts"]:
            if (
                member["key"] not in charts
                or charts[member["key"]].get("tab", charts[member["key"]].get("family"))
                != group["tab"]
            ):
                message = f"Chart group {key} has an unknown or cross-tab chart."
                raise DataLensUtilsError(message)
    connections = read_config("UI/links/connections.json")
    if set(connections) != set(selectors):
        message = "UI links must declare recipients for every selector exactly once."
        raise DataLensUtilsError(message)
    selectors = {key: {**value, "recipients": connections[key]} for key, value in selectors.items()}
    if set(layouts) != set(tabs):
        message = "Every dashboard tab must have exactly one layout."
        raise DataLensUtilsError(message)
    if set(titles) - set(tabs) or set(texts) - set(tabs) or set(aliases) - set(tabs):
        message = "UI titles, texts, or aliases refer to unknown tabs."
        raise DataLensUtilsError(message)
    for key, definition in selectors.items():
        _validate_selector(
            key,
            definition,
            tabs=tabs,
            charts=charts,
            datasets=datasets,
            selectors=selectors,
            chart_groups=chart_groups,
        )
    grouped = Counter(member for group in groups.values() for member in group.get("members", []))
    if any(count != 1 for count in grouped.values()):
        message = "Each grouped selector can belong to exactly one group."
        raise DataLensUtilsError(message)
    for key, group in groups.items():
        if group.get("tab") not in tabs or not group.get("members"):
            message = f"Selector group {key} needs a known tab and members."
            raise DataLensUtilsError(message)
        for member in group["members"]:
            if (
                member not in selectors
                or selectors[member].get("group") != key
                or selectors[member]["tab"] != group["tab"]
            ):
                message = f"Selector group {key} has an invalid member: {member}"
                raise DataLensUtilsError(message)
    for key, definition in selectors.items():
        if bool(definition.get("group")) != (key in grouped):
            message = f"Selector {key} has an undeclared group membership."
            raise DataLensUtilsError(message)
    unknown_params = set(chart_params) - {
        key for key, chart in charts.items() if chart.get("type") != "selector"
    }
    if unknown_params:
        message = f"Widget defaults refer to unknown charts: {sorted(unknown_params)}"
        raise DataLensUtilsError(message)
    for key, defaults in chart_params.items():
        chart = charts[key]
        names = (
            {value["name"] for value in chart.get("params", [])}
            if chart["family"] == "ql"
            else set(chart.get("parameter_bindings", {}))
        )
        if chart["family"] in {"ql", "editor"} and set(defaults) - names:
            message = (
                f"Widget {key} defaults refer to undeclared parameters: "
                f"{sorted(set(defaults) - names)}"
            )
            raise DataLensUtilsError(message)
    contents, all_ids = ({}, [])
    for tab, layout in layouts.items():
        _validate_positions(
            tab, layout, allow_overlaps=allow_overlaps or tabs[tab].get("preserve_layout", False)
        )
        content = {
            "titles": {
                key: {**value, "at": layout.get(key)} for key, value in titles.get(tab, {}).items()
            },
            "texts": {
                key: {**value, "at": layout.get(key)} for key, value in texts.get(tab, {}).items()
            },
            "selectors": {},
            "selector_groups": {},
            "charts": {},
            "chart_groups": {
                key: {**group, "at": layout.get(key)}
                for key, group in chart_groups.items()
                if group["tab"] == tab
            },
            "aliases": aliases.get(tab, []),
            "chart_params": {},
        }
        for key, definition in selectors.items():
            if definition["tab"] == tab and (not definition.get("group")):
                wrapper = key if definition["source"]["kind"] == "editor" else f"{key}_control"
                content["selectors"][wrapper] = {**definition, "at": layout.get(wrapper)}
                if wrapper != key:
                    all_ids.append(key)
        for key, group in groups.items():
            if group["tab"] == tab:
                content["selector_groups"][key] = {
                    **group,
                    "at": layout.get(key),
                    "definitions": {member: selectors[member] for member in group["members"]},
                }
                all_ids.extend(group["members"])
        for key, chart in charts.items():
            if (
                chart.get("tab", chart.get("family")) == tab
                and chart.get("type") != "selector"
                and (key not in grouped_charts)
            ):
                content["charts"][key] = layout.get(key)
                content["chart_params"][key] = chart_params.get(key, {})
        declared = [
            key
            for kind in (
                "titles",
                "texts",
                "selectors",
                "selector_groups",
                "charts",
                "chart_groups",
            )
            for key in content[kind]
        ]
        if len(declared) != len(set(declared)) or set(declared) != set(layout):
            message = (
                f"UI layout {tab} must position each declared item exactly once; "
                f"missing={sorted(set(declared) - set(layout))}, "
                f"unknown={sorted(set(layout) - set(declared))}"
            )
            raise DataLensUtilsError(message)
        for group in content["aliases"]:
            if len(group) < _MIN_ALIAS_MEMBERS or len(
                {
                    (member.get("dataset"), member.get("field"), member.get("parameter"))
                    for member in group
                }
            ) != len(group):
                message = f"Alias group {tab} requires at least two distinct fields or parameters."
                raise DataLensUtilsError(message)
            for member in group:
                if "parameter" in member:
                    if not any(
                        value["source"].get("param_name") == member["parameter"]
                        for value in selectors.values()
                    ):
                        message = f"Alias group {tab} refers to an unknown manual parameter."
                        raise DataLensUtilsError(message)
                elif member.get("dataset") not in datasets or member.get(
                    "field"
                ) not in _declared_fields(datasets[member["dataset"]]):
                    message = f"Alias group {tab} refers to an unknown dataset field."
                    raise DataLensUtilsError(message)
        contents[tab] = content
        all_ids.extend(declared)
    if len(all_ids) != len(set(all_ids)):
        message = "Dashboard item, wrapper, and selector member keys must be document-wide unique."
        raise DataLensUtilsError(message)
    return contents
