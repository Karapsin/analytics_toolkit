"""Public SDK dashboard serialization, metadata fidelity and draft recovery."""

import json
from contextlib import nullcontext

import pytest
from analytics_toolkit.datalens_utils import bi_engine, planning
from analytics_toolkit.datalens_utils.bi_dashboard import dashboard_issues, reconcile_dashboard
from analytics_toolkit.datalens_utils.bi_pull import pull_resources
from analytics_toolkit.datalens_utils.bi_pull_ui import pull_dashboard
from analytics_toolkit.datalens_utils.errors import DataLensCapabilityError
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore
from datalens_sdk import DashboardChartTab, DashboardTab

from tests.datalens_utils._support.bi import dataset, write
from tests.datalens_utils._support.sdk import MemoryDataLens


def setup_dashboard(project, *, grouped=False):
    api = MemoryDataLens()
    root = project.paths.project_root
    definition = load_registry().resources["chart:trend"].definition
    chart = (
        api.client.create.wizard_chart.line(name="Trend", location=api.folder)
        .dataset(dataset())
        .x([dataset().fields.by_name("category")])
        .y([dataset().fields.by_name("Amount")])
        .build()
    )
    definition["id"] = chart.id
    write(root, "configs/DL objects/charts/wizard/line/trend.json", definition)
    tab = DashboardTab("Main", tab_id="main")
    if grouped:
        tab.add_chart_group(
            [
                DashboardChartTab(chart=chart.id, title="A"),
                DashboardChartTab(chart=chart.id, title="B"),
            ],
            item_id="variants",
            at=(0, 0, 12, 6),
        )
        write(
            root,
            "configs/UI/layout.json",
            {"main": {"variants": [0, 0, 12, 6], "region_control": [0, 6, 12, 2]}},
        )
        write(
            root,
            "configs/UI/chart_groups.json",
            {
                "variants": {
                    "tab": "main",
                    "charts": [
                        {
                            "key": "a",
                            "chart": "trend",
                            "title": "A",
                            "params": {"period_type": "month"},
                        },
                        {
                            "key": "b",
                            "chart": "trend",
                            "title": "B",
                            "params": {"period_type": "year"},
                        },
                    ],
                }
            },
        )
        write(
            root,
            "configs/UI/selectors/region.json",
            {
                "key": "region",
                "tab": "main",
                "title": "Region",
                "source": {"kind": "dataset", "dataset": "sales", "field": "category"},
                "control": {"element": "select"},
            },
        )
        write(root, "configs/UI/links/connections.json", {"region": ["variants/a"]})
    else:
        tab.add_chart(chart.id, title="Trend", item_id="trend", at=(0, 0, 12, 6))
    tab.add_text("Unmanaged", item_id="foreign", at=(12, 0, 12, 2))
    dashboard = (
        api.client.create.dashboard(name="Dashboard", location=api.folder).add_tab(tab).build()
    )
    write(root, "configs/DL objects/dashboard.json", {"id": dashboard.id})
    registry = load_registry()
    store = BIResourceStore(registry)
    store.set_location(api.folder)
    return api, registry, store, {"trend": chart}, {"sales": dataset()}


@pytest.mark.parametrize("grouped", [False, True])
def test_reconcile_public_sdk_reuses_ids_and_is_noop(bi_project, grouped):
    api, registry, store, charts, datasets = setup_dashboard(bi_project, grouped=grouped)
    resource = registry.resources["dashboard:main"]
    definitions = {"trend": registry.resources["chart:trend"].definition}
    value = reconcile_dashboard(api.client, store, resource, charts, datasets, definitions)
    assert not dashboard_issues(value, resource.definition, charts, datasets, definitions)
    assert any(item.id == "foreign" for item in value.tabs[0].items)
    store.remember("dashboard:main", value)
    before = list(api.writes)
    repeated = reconcile_dashboard(api.client, store, resource, charts, datasets, definitions)
    assert repeated.id == value.id
    assert api.writes == before
    if grouped:
        baseline = resource.files
        pulled = pull_dashboard(
            value, baseline, datasets, {"trend": {**definitions["trend"], "id": charts["trend"].id}}
        )
        assert "variants/a" in pulled["configs/UI/links/connections.json"]["region"]
        assert "variants/b" not in pulled["configs/UI/links/connections.json"]["region"]
        assert [
            member["key"] for member in pulled["configs/UI/chart_groups.json"]["variants"]["charts"]
        ] == ["a", "b"]
    api.client.close()


def test_interrupted_dashboard_draft_resumes_without_create(bi_project):
    api, registry, store, charts, datasets = setup_dashboard(bi_project)
    resource = registry.resources["dashboard:main"]
    definitions = {"trend": registry.resources["chart:trend"].definition}
    api.fail_after_operation = "updateDashboard"
    with pytest.raises(RuntimeError, match="failed re-fetch"):
        reconcile_dashboard(api.client, store, resource, charts, datasets, definitions)
    checkpoint = json.loads(store.path.read_text())
    assert checkpoint["resources"]["dashboard"]["dashboard_rebuild"]
    assert checkpoint["resources"]["dashboard"]["pending_write"]
    recovered = BIResourceStore(registry)
    recovered.set_location(api.folder)
    value = reconcile_dashboard(api.client, recovered, resource, charts, datasets, definitions)
    assert not dashboard_issues(value, resource.definition, charts, datasets, definitions)
    assert sum(operation == "createDashboard" for operation, _ in api.writes) == 1
    assert sum(operation == "updateDashboard" for operation, _ in api.writes) == 3
    api.client.close()


def test_whole_float_recipe_coordinates_validate_without_rewriting(bi_project):
    path = bi_project.paths.project_root / "configs/UI/layout.json"
    write(
        bi_project.paths.project_root,
        "configs/UI/layout.json",
        {"main": {"trend": [0.0, 0.0, 12.0, 6.0]}},
    )
    before = path.read_bytes()
    assert bi_project.validate()["coverage"]["schema_version"] == 2
    assert path.read_bytes() == before


def test_pull_keeps_changed_direct_widget_metadata_and_unchanged_omissions(bi_project):
    api, registry, store, charts, datasets = setup_dashboard(bi_project)
    resource = registry.resources["dashboard:main"]
    definitions = {"trend": registry.resources["chart:trend"].definition}
    dashboard = reconcile_dashboard(api.client, store, resource, charts, datasets, definitions)
    chart_definitions = {"trend": {**definitions["trend"], "id": charts["trend"].id}}
    untouched = pull_dashboard(dashboard, resource.files, datasets, chart_definitions)
    assert "configs/UI/widgets.json" not in untouched
    for item in dashboard.data["tabs"][0]["items"]:
        if item["id"] == "trend":
            member = item["data"]["tabs"][0]
            member.update(
                title="Remote title", params={"period_type": ["year"]}, enableActionParams=True
            )
    incoming = pull_dashboard(dashboard, resource.files, datasets, chart_definitions)
    widget = incoming["configs/UI/widgets.json"]["trend"]
    assert widget["title"] == "Remote title"
    assert widget["params"] == {"period_type": ["year"]}
    assert widget["enable_action_params"]
    assert widget["chart"] == "trend"
    assert "trend" in incoming["configs/UI/links/connections.json"]
    store.remember("dashboard:main", dashboard)
    store.remember("chart:trend", charts["trend"])
    pull_resources(
        api.client,
        store,
        registry,
        {"dashboard:main"},
        {
            "dashboard:main": dashboard,
            "chart:trend": charts["trend"],
            "dataset:sales": datasets["sales"],
        },
        branch="published",
    )
    assert bi_project.validate()["coverage"]["schema_version"] == 2
    assert (
        json.loads((bi_project.paths.project_root / "configs/UI/widgets.json").read_text())[
            "trend"
        ]["title"]
        == "Remote title"
    )
    api.client.close()


def test_fractional_authoring_reports_public_sdk_requirement(bi_project):
    write(
        bi_project.paths.project_root,
        "configs/UI/layout.json",
        {"main": {"trend": [0.5, 0.0, 12.0, 6.0]}},
    )
    with pytest.raises(DataLensCapabilityError, match="typed SDK layout contract"):
        bi_project.validate()
    assert not bi_project.paths.runtime_root.exists()


def test_plan_detects_chart_title_change_in_affected_dashboard(bi_project, monkeypatch):
    api, registry, store, charts, datasets = setup_dashboard(bi_project)
    resource = registry.resources["dashboard:main"]
    definitions = {"trend": registry.resources["chart:trend"].definition}
    dashboard = reconcile_dashboard(api.client, store, resource, charts, datasets, definitions)
    store.remember("dashboard:main", dashboard)
    before_writes = list(api.writes)
    checkpoint = store.path.read_bytes()
    definitions["trend"]["title"] = "Updated placement title"
    write(
        bi_project.paths.project_root,
        "configs/DL objects/charts/wizard/line/trend.json",
        definitions["trend"],
    )
    registry = load_registry()
    monkeypatch.setattr(planning, "datalens_client", lambda: nullcontext(api.client))
    monkeypatch.setattr(
        bi_engine,
        "read_entities",
        lambda *args, **kwargs: {
            "dataset:sales": datasets["sales"],
            "chart:trend": charts["trend"],
            "dashboard:main": dashboard,
        },
    )
    report = planning.plan_v2(registry, resource_keys=["chart:trend"])
    action = next(value for value in report["actions"] if value["resource"] == "dashboard:main")
    assert action["action"] == "update"
    assert action["will_write"]
    assert store.path.read_bytes() == checkpoint
    assert api.writes == before_writes


def test_shared_controls_actions_settings_and_presentation_roundtrip(bi_project):
    api, registry, _store, charts, datasets = setup_dashboard(bi_project, grouped=True)
    root = bi_project.paths.project_root
    write(
        root,
        "configs/UI/tabs.json",
        {"other": {"title": "Other", "hidden": True}, "main": {"title": "Main"}},
    )
    groups = {
        "shared": {
            "tab": "main",
            "members": ["region", "manual"],
            "show_on_tabs": "all",
            "apply_button": True,
            "reset_button": True,
            "show_group_name": True,
            "auto_height": True,
            "update_on_change": False,
            "border_radius": 8,
        }
    }
    write(root, "configs/UI/selectors/selector_groups.json", groups)
    write(
        root,
        "configs/UI/selectors/region.json",
        {
            "key": "region",
            "tab": "main",
            "group": "shared",
            "title": "Region",
            "affects": "all_tabs",
            "source": {"kind": "dataset", "dataset": "sales", "field": "category"},
            "control": {
                "element": "select",
                "default_value": ["West"],
                "multiselect": True,
                "show_title": True,
                "hint": "Choose region",
            },
        },
    )
    write(
        root,
        "configs/UI/selectors/manual.json",
        {
            "key": "manual",
            "tab": "main",
            "group": "shared",
            "title": "Period",
            "affects": ["other"],
            "source": {"kind": "manual", "param_name": "period_type"},
            "control": {
                "element": "select",
                "default_value": "month",
                "options": [{"title": "Month", "value": "month"}],
            },
        },
    )
    write(
        root,
        "configs/UI/widgets.json",
        {
            "direct": {
                "chart": "trend",
                "tab": "other",
                "title": "Other trend",
                "params": {"period_type": "month"},
                "enable_action_params": True,
                "presentation": {
                    "show_title": True,
                    "auto_height": True,
                    "description": "Placement",
                    "hint": "Hint",
                },
            }
        },
    )
    write(
        root,
        "configs/UI/layout.json",
        {
            "other": {"direct": [0, 0, 12, 6], "shared": [0, 6, 12, 3]},
            "main": {"variants": [0, 0, 12, 6], "shared": [0, 6, 12, 3]},
        },
    )
    write(
        root,
        "configs/UI/links/connections.json",
        {"region": ["variants/a", "direct", "manual"], "manual": ["direct"], "direct": ["region"]},
    )
    write(
        root,
        "configs/UI/links/aliases.json",
        {"main": [[{"dataset": "sales", "field": "category"}, {"parameter": "region"}]]},
    )
    original = registry.resources["dashboard:main"].definition
    write(
        root,
        "configs/DL objects/dashboard.json",
        {
            **original,
            "description": "Description",
            "support_description": "Support",
            "access_description": "Access",
            "hide_tabs": True,
            "settings": {
                "silent_loading": True,
                "expand_toc": True,
                "hide_dash_title": True,
                "autoupdate_interval": 120,
                "max_concurrent_requests": 4,
            },
            "global_parameters": {"period_type": "month", "discard": None},
        },
    )
    registry = load_registry()
    store = BIResourceStore(registry)
    store.set_location(api.folder)
    resource = registry.resources["dashboard:main"]
    definitions = {"trend": registry.resources["chart:trend"].definition}
    value = reconcile_dashboard(api.client, store, resource, charts, datasets, definitions)
    assert not dashboard_issues(value, resource.definition, charts, datasets, definitions)
    assert [tab.id for tab in value.tabs] == ["other", "main"]
    assert value.tabs[0].hidden
    assert value.data["settings"]["dependentSelectors"]
    assert all(any(item.id == "shared" for item in tab.global_items) for tab in value.tabs)
    pulled = pull_dashboard(
        value,
        resource.files,
        datasets,
        {"trend": {**definitions["trend"], "id": charts["trend"].id}},
    )
    assert pulled["definition"]["description"] == "Description"
    assert pulled["configs/UI/widgets.json"]["direct"]["enable_action_params"]
    store.remember("dashboard:main", value)
    before = list(api.writes)
    reconcile_dashboard(api.client, store, resource, charts, datasets, definitions)
    assert api.writes == before
    api.client.close()
