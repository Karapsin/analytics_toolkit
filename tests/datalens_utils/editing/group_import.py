"""Grouped chart import retains presentation but rejects structural drift."""

import copy
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.create import (
    create_dashboard,
    create_tabs,
)
from analytics_toolkit.datalens_utils.editing.pull import pull_ui
from analytics_toolkit.datalens_utils.editing.state import configuration
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError


@pytest.mark.parametrize("change", ["unknown chart", "missing chart", "parameters"])
def test_group_import_preserves_parameters_and_rejects_changed_members(financial_case, change):
    case = financial_case
    charts = case.build_charts()
    dashboard = create_dashboard(
        context=case.context,
        path=case.backend.folder.key + "Report",
        tabs=create_tabs(definitions=case.tabs),
        description="Offline financial report",
        hide_tabs=True,
    )
    dashboard = case.populate(dashboard, charts)
    definitions = {key: {**value, "id": charts[key].id} for key, value in case.definitions.items()}
    files = configuration()["dashboard"]["files"]
    files["configs/UI/layout.json"] = case.fixture_layout
    data = copy.deepcopy(dashboard.data)
    group_keys = files["configs/UI/chart_groups.json"]
    widget = next(item for tab in data["tabs"] for item in tab["items"] if item["id"] in group_keys)
    tabs = widget["data"]["tabs"]
    if change == "unknown chart":
        tabs[0]["chartId"] = "unknown"
    elif change == "missing chart":
        tabs.pop()
    else:
        tabs[0]["params"] = {"alpha": ["0.05"]}
    view = SimpleNamespace(
        raw=dashboard.raw,
        data=data,
        tabs=type(dashboard)(
            id=dashboard.id, data=data, raw={"data": data}, installation="yacloud"
        ).tabs,
    )
    if change == "parameters":
        result = pull_ui(view, files, case.datasets, definitions)
        assert result["configs/UI/chart_groups.json"][widget["id"]]["charts"][0]["params"] == {
            "alpha": ["0.05"]
        }
    else:
        with pytest.raises(DataLensUtilsError, match=r"chart binding|members"):
            pull_ui(view, files, case.datasets, definitions)
