"""Selector metadata and display scope survive captured-project maintenance."""

import json

import pytest
from analytics_toolkit.datalens_utils import bi_engine

from tests.datalens_utils._support.captured_fidelity import assert_readable_only
from tests.datalens_utils._support.captured_import import imported_project, run_import
from tests.datalens_utils._support.captured_selectors import selector_source


def mutate_selector(data, feature):
    wrappers = [
        item
        for tab in data["tabs"]
        for item in (*tab.get("items", []), *tab.get("globalItems", []))
        if item["id"] == "filters"
    ]
    for wrapper in wrappers:
        member = wrapper["data"]["group"][0]
        if feature == "malformed-default":
            member["source"].update(operation="EQ", defaultValue=[17])
            continue
        if feature == "option-context":
            member["source"]["elementType"] = "input"
            member["defaults"]["choice"] = ""
            continue
        mutations = {
            "wrapper": (wrapper["data"], "futureSetting", {"keep": "authored"}),
            "member": (member, "futureSetting", {"keep": "authored"}),
            "title": (member, "title", {"text": "Authored title"}),
            "source": (member["source"], "futureSetting", {"keep": "authored"}),
            "width": (member, "width", "50%"),
            "placement": (member, "placementMode", "manual"),
            "namespace": (member, "namespace", "authored-namespace"),
            "hint": (member["source"], "hint", "Hidden authored hint"),
            "default-extra": (member["defaults"], "another_parameter", ["A"]),
            "default-value": (member["defaults"], "choice", ["A"]),
            "option": (member["source"]["acceptableValues"][0], "futureSetting", True),
            "option-shape": (member["source"], "acceptableValues", ["unrepresented"]),
            "source-flag": (member["source"], "required", "dynamic"),
            "source-text": (member["source"], "fieldName", 17),
            "member-identity": (member, "id", None),
            "group-shape": (wrapper["data"], "group", [member, "unrepresented"]),
        }
        target, name, value = mutations[feature]
        target[name] = value


@pytest.mark.parametrize("scope", ["local", "shared"])
@pytest.mark.parametrize(
    "feature",
    [
        "wrapper",
        "member",
        "title",
        "source",
        "width",
        "placement",
        "namespace",
        "hint",
        "default-extra",
        "default-value",
        "option",
        "option-shape",
        "source-flag",
        "member-identity",
        "group-shape",
        "malformed-default",
        "source-text",
        "option-context",
    ],
)
def test_unsupported_selector_metadata_stays_readable_and_blocks_writes(
    import_source, tmp_path, scope, feature
):
    selector_source(import_source, scope, False)
    assert_readable_only(import_source, tmp_path, lambda data: mutate_selector(data, feature))


@pytest.mark.parametrize("default", ["A", ["A"]])
@pytest.mark.parametrize("operation", [None, "EQ"])
def test_supported_selector_defaults_keep_their_encoding(
    import_source, tmp_path, default, operation
):
    selector_source(import_source, "local", False, default_value=default, operation=operation)
    report = run_import(import_source, tmp_path, dry_run=False)
    assert report["resources"]["dashboard:main"]["editable"]
    project = imported_project(import_source, tmp_path)
    writes = list(import_source.api.writes)
    project.pull(resource_keys=["dashboard:main"])
    assert not project.verify(full=True)["mismatches"]
    assert import_source.api.writes == writes


def test_all_tabs_selector_scope_includes_a_new_local_tab(import_source, tmp_path, monkeypatch):
    dashboard = selector_source(import_source, "shared", False)
    report = run_import(import_source, tmp_path, dry_run=False)
    assert report["resources"]["dashboard:main"]["editable"]
    project = imported_project(import_source, tmp_path)
    root = project.paths.project_root
    groups = json.loads((root / "configs/UI/selectors/selector_groups.json").read_text())
    assert groups["filters"]["show_on_tabs"] == "all"
    path = root / "configs/UI/tabs.json"
    tabs = json.loads(path.read_text())
    tabs["three"] = {"title": "New tab"}
    path.write_text(json.dumps(tabs))
    path = root / "configs/UI/layout.json"
    layout = json.loads(path.read_text())
    layout["two"]["filters"][1] = 0
    layout["three"] = {"filters": [0, 0, 12, 2]}
    path.write_text(json.dumps(layout))
    import_source.client.get.dashboard = import_source.api.client.get.dashboard
    monkeypatch.setattr(bi_engine, "resolve_location", lambda *_args: import_source.api.folder)
    project.apply(resource_keys=["dashboard:main"])
    current = import_source.api.client.get.dashboard(by_id=dashboard.id, branch="published")
    new_tab = next(tab for tab in current.tabs if tab.id == "three")
    control = next(control for control in new_tab.controls if control.id == "filters")
    assert control.item.data["impactType"] == "allTabs"
    import_source.entities[current.id] = current
    assert not project.verify(full=True)["mismatches"]
