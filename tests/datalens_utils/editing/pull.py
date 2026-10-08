"""Remote extraction preserves templates and rejects unsupported structural changes."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.editing import commands, pull
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError

from tests.datalens_utils._support.sdk import configured_datasets


@pytest.fixture
def variants(session_state):
    from tests.datalens_utils.editing.adapters import EditingAdapterTests  # noqa: PLC0415

    case = EditingAdapterTests()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


@pytest.mark.parametrize("as_list", [False, True])
def test_dataset_pull_extracts_direct_calculated_and_parameter_fields(as_list):
    definition = {
        "name": "Sales",
        "fields": {"raw": {"cast": "string"}},
        "calculations": {"Total": {"formula": "1", "cast": "integer", "kind": "MEASURE"}},
    }
    dataset = configured_datasets({"sales": definition})["sales"]
    expected = copy.deepcopy(definition)
    expected["calculations"] = [] if as_list else {}
    result = pull.pull_dataset(dataset, expected)
    assert result["fields"]["raw"]["title"] == "raw"
    assert result["calculations"][0 if as_list else "Total"]["formula"] == "1"
    assert result["description"] == ""
    with pytest.raises(DataLensUtilsError, match="source changes"):
        pull.pull_dataset(dataset, {"fields": {"missing": {}}, "name": "Sales"})
    fields = Mock()
    fields.__iter__ = Mock(return_value=iter([]))
    fields.by_name.return_value = SimpleNamespace(cast="string", default_value="North")
    result = pull.pull_dataset(
        SimpleNamespace(name="Sales", description="Updated", fields=fields),
        {"parameters": {"region": {}}},
    )
    assert result["parameters"]["region"] == {"type": "string", "default": "North"}


@pytest.mark.parametrize(
    "problem",
    ["editor tab", "ql query", "ql slot", "wizard type", "wizard field", "table settings"],
)
def test_chart_pull_imports_supported_values_and_rejects_unknown_shape(variants, problem):
    case = variants
    family = (
        "js_table"
        if problem == "editor tab"
        else "ql_flat_table"
        if problem.startswith("ql")
        else "wizard_flat_table"
    )
    chart = case.charts[family]
    definition = copy.deepcopy(case.gallery.definitions[family])
    if problem == "editor tab":
        definition["scripts"]["missing"] = "assets/missing.js"
        with patch.object(pull.editor, "validate_change"), pytest.raises(
            DataLensUtilsError, match="Editor tab"
        ):
            pull.pull_chart(chart, definition, case.gallery.datasets, case.gallery.context)
    elif problem == "ql query":
        view = SimpleNamespace(
            name=chart.name, description="Imported", query_value="SELECT 42", params=chart.params
        )
        definition.pop("description", None)
        with patch.object(pull.ql, "validate_change"), patch.object(
            pull.ql, "_actual_columns", return_value=[]
        ):
            result, assets = pull.pull_chart(
                view, definition, case.gallery.datasets, case.gallery.context
            )
        assert assets[definition["query_file"]] == "SELECT 42"
        assert result["description"] == "Imported"
    elif problem == "ql slot":
        with patch.object(pull.ql, "_actual_columns", return_value=None), pytest.raises(
            DataLensUtilsError, match="QL slot"
        ):
            pull.pull_chart(chart, definition, case.gallery.datasets, case.gallery.context)
    elif problem == "wizard type":
        definition["type"] = "indicator"
        with pytest.raises(DataLensUtilsError, match="visualization"):
            pull.pull_chart(chart, definition, case.gallery.datasets, case.gallery.context)
    else:
        data = copy.deepcopy(chart.data)
        definition["totals"] = False
        settings = data["visualization"].setdefault("chartSettings", {})
        settings.update(totals="on", pagination="on", limit=25)
        data["visualization"].setdefault("x", {})["settings"] = {
            "title": "on",
            "titleValue": "Axis",
        }
        if problem == "wizard field":
            slot = next(iter(definition["fields"]))
            data["visualization"][slot]["items"][0]["guid"] = "unknown"
        view = SimpleNamespace(
            name=chart.name,
            description=chart.description,
            visualization_id=chart.visualization_id,
            data=data,
        )
        registered = pull.wizard.registered_local_fields(chart)
        with patch.object(pull.wizard, "registered_local_fields", return_value=registered):
            if problem == "wizard field":
                with pytest.raises(DataLensUtilsError, match="Unknown field GUID"):
                    pull.pull_chart(view, definition, case.gallery.datasets, case.gallery.context)
            else:
                result, _ = pull.pull_chart(
                    view, definition, case.gallery.datasets, case.gallery.context
                )
                assert result["totals"] is True
                assert result["settings"]["pagination"]["limit"] == 25
                assert result["axis_titles"]["x"] == "Axis"


def test_empty_remote_description_and_label_only_format_are_preserved(variants):
    case = variants
    chart = case.charts["wizard_line"]
    definition = copy.deepcopy(case.gallery.definitions["wizard_line"])
    definition.pop("description", None)
    data = copy.deepcopy(chart.data)
    for section in data["visualization"].values():
        if isinstance(section, dict):
            for field in section.get("items", []):
                field["formatting"] = {"labelMode": "absolute"}
    view = SimpleNamespace(
        name=chart.name, description="", visualization_id=chart.visualization_id, data=data
    )
    with patch.object(
        pull.wizard,
        "registered_local_fields",
        return_value=pull.wizard.registered_local_fields(chart),
    ):
        result, _ = pull.pull_chart(view, definition, case.gallery.datasets, case.gallery.context)
    assert "description" not in result
    assert result["formats"] == {}
    assert pull.field_names(case.gallery.datasets)


@pytest.mark.parametrize(
    "problem",
    [
        "unknown tab",
        "missing layout",
        "changed binding",
        "unknown selector",
        "unknown field",
        "aliases",
    ],
)
def test_ui_pull_rejects_structural_drift_and_keeps_unmanaged_tabs(variants, problem):
    case = variants
    dashboard, _, _ = case.build_dashboard()
    units = pull_files = copy.deepcopy(commands.configuration()["dashboard"]["files"])
    tabs = list(dashboard.tabs)
    selected = tabs[0]
    if problem == "unknown tab":
        tabs.append(SimpleNamespace(id="unmanaged"))
    if problem == "missing layout":
        pull_files["configs/UI/layout.json"][selected.id]["missing"] = [0, 0, 1, 1]
    if problem == "changed binding":
        item = selected.items[0]
        changed = SimpleNamespace(
            id=next(iter(case.charts)),
            item_type="widget",
            data={"tabs": [{"chartId": "different"}]},
        )
        selected = SimpleNamespace(
            id=selected.id,
            title=selected.title,
            hidden=selected.hidden,
            layout=selected.layout,
            items=[changed, item],
            global_items=[],
            controls=[],
            connections=selected.connections,
            aliases=selected.aliases,
        )
        tabs[0] = selected
    if problem in {"unknown selector", "unknown field"}:
        member = SimpleNamespace(id="unknown", title="Unknown")
        if problem == "unknown field":
            original = next(
                value
                for key, value in units.items()
                if key.startswith("configs/UI/selectors/")
                and value.get("source", {}).get("kind") == "dataset"
            )
            member.id = original["key"]
            member.source_type = "dataset"
            member.source = SimpleNamespace(
                element_type="input",
                multiselect=False,
                is_range=False,
                required=False,
                default_value=None,
                operation=None,
                raw={},
                dataset_field_id="unknown",
            )
        selected = SimpleNamespace(
            id=selected.id,
            title=selected.title,
            hidden=selected.hidden,
            layout=selected.layout,
            items=selected.items,
            global_items=selected.global_items,
            controls=[SimpleNamespace(members=[member])],
            connections=selected.connections,
            aliases=selected.aliases,
        )
        tabs[0] = selected
    if problem == "aliases":
        selected.aliases["default"] = [["unknown-a", "unknown-b"]]
    view = SimpleNamespace(tabs=tabs, raw=dashboard.raw, data=dashboard.data)
    definitions = {
        role: {**value, "id": case.charts[role].id}
        for role, value in case.gallery.definitions.items()
    }
    if problem in {"missing layout", "changed binding", "unknown field"}:
        with pytest.raises(DataLensUtilsError):
            pull.pull_ui(view, pull_files, case.gallery.datasets, definitions)
    else:
        result = pull.pull_ui(view, pull_files, case.gallery.datasets, definitions)
        if problem == "aliases":
            assert result["configs/UI/links/aliases.json"][selected.id] == [
                [{"parameter": "unknown-a"}, {"parameter": "unknown-b"}]
            ]
