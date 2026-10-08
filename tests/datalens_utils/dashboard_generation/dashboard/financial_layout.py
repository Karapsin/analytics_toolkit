# Конфигурация, сериализация и восстановление без облака и доступа к базам.

import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard import configuration
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.create import (
    create_dashboard,
    create_tabs,
)
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.populate import (
    populate_dashboard,
)
from analytics_toolkit.datalens_utils.editing.commands import validate_import
from analytics_toolkit.datalens_utils.editing.pull import pull_chart, pull_ui
from analytics_toolkit.datalens_utils.editing.state import configuration as edit_configuration
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.settings import (
    read_config,
)
from analytics_toolkit.datalens_utils.validation.charts import check_chart
from analytics_toolkit.datalens_utils.validation.dashboard import (
    dashboard_issues,
    normalized_edges,
)
from analytics_toolkit.datalens_utils.validation.recipe import validate_recipe

from tests.datalens_utils._support.financial import FinancialCase


class FinancialReportTests(FinancialCase):
    def test_financial_tables_import_preserves_order_titles_formats_and_filters(self):
        charts = self.build_charts()
        for key, chart in charts.items():
            definition, assets = pull_chart(
                chart, self.definitions[key], self.datasets, self.context
            )
            assert assets == {}
            assert definition["fields"] == self.definitions[key]["fields"]
            assert not definition["show_title"]
            assert definition["title"] == self.definitions[key]["title"]
            assert chart.data["visualization"]["chartSettings"]["titleMode"] == "hide"
            assert (
                check_chart(
                    chart, context=self.context, datasets=self.datasets, definition=definition
                )
                == []
            )

    def test_financial_ui_import_preserves_period_date_dependencies_and_alpha(self):
        charts = self.build_charts()
        dashboard = create_dashboard(
            context=self.context,
            path=self.backend.folder.key + "Financial report",
            tabs=create_tabs(definitions=self.tabs),
            description="Offline financial report",
            hide_tabs=True,
        )
        dashboard = self.populate(dashboard, charts)
        definitions = copy.deepcopy(self.definitions)
        for role, chart in charts.items():
            definitions[role]["id"] = chart.id
        files = edit_configuration()["dashboard"]["files"]
        files["configs/UI/layout.json"] = copy.deepcopy(self.fixture_layout)
        pulled = pull_ui(dashboard, files, self.datasets, definitions)
        for key, value in files.items():
            if key.startswith("configs/UI/"):
                assert pulled[key] == value, key
        before = len(self.backend.writes)
        contents = copy.deepcopy(self.contents)
        members = contents["month"]["selector_groups"]["month_filters"]["definitions"]
        members["alpha_selector"]["control"]["default_value"] = "0.05"
        again = populate_dashboard(
            context=self.context,
            dashboard=dashboard,
            datasets=self.datasets,
            charts=charts,
            tab_definitions=self.tabs,
            contents=contents,
            chart_definitions=self.definitions,
            description="Offline financial report",
            hide_tabs=True,
        )
        assert len(self.backend.writes) - before == 1
        assert (
            dashboard_issues(
                again,
                tab_definitions=self.tabs,
                contents=contents,
                datasets=self.datasets,
                charts=charts,
                chart_definitions=self.definitions,
                description="Offline financial report",
                hide_tabs=True,
            )
            == []
        )

    def test_pull_preserves_ui_scope_multiselect_and_update_behavior(self):
        charts = self.build_charts()
        dashboard = create_dashboard(
            context=self.context,
            path=self.backend.folder.key + "Financial report",
            tabs=create_tabs(definitions=self.tabs),
            description="Offline financial report",
            hide_tabs=True,
        )
        dashboard = self.populate(dashboard, charts)
        document = self.backend.documents[dashboard.id]["entry"]
        month = next(tab for tab in document["data"]["tabs"] if tab["id"] == "month")
        group = next(item["data"] for item in month["items"] if item["id"] == "month_filters")
        group.update(impactType="currentTab", impactTabsIds=["month"], updateControlsOnChange=False)
        for member in group["group"]:
            member["impactType"] = "asGroup"
            if member["id"] == "month_group_selector":
                member["source"]["multiselectable"] = True
                member["source"].pop("operation", None)
        dashboard = self.backend.client.get.dashboard(by_id=dashboard.id)
        files = edit_configuration()["dashboard"]["files"]
        files["configs/UI/layout.json"] = copy.deepcopy(self.fixture_layout)
        path = next(
            key
            for key, value in files.items()
            if isinstance(value, dict) and value.get("key") == "month_group_selector"
        )
        files[path]["control"]["operation"] = "EQ"
        pulled = pull_ui(
            dashboard,
            files,
            self.datasets,
            {key: {**value, "id": charts[key].id} for key, value in self.definitions.items()},
        )
        assert not pulled["configs/UI/selectors/selector_groups.json"]["month_filters"][
            "update_on_change"
        ]
        assert pulled[path]["control"]["multiselect"]
        assert "operation" not in pulled[path]["control"]
        contents = copy.deepcopy(self.contents)
        wanted = contents["month"]["selector_groups"]["month_filters"]
        wanted["update_on_change"] = False
        contents["month"]["selectors"]["month_group_selector_control"]["control"] = pulled[path][
            "control"
        ]
        assert (
            dashboard_issues(
                dashboard,
                tab_definitions=self.tabs,
                contents=contents,
                datasets=self.datasets,
                charts=charts,
                chart_definitions=self.definitions,
                description="Offline financial report",
                hide_tabs=True,
            )
            == []
        )

    def test_staged_import_accepts_existing_layout_warnings_without_changing_files(self):
        charts = self.build_charts()
        dashboard = create_dashboard(
            context=self.context,
            path=self.backend.folder.key + "Financial report",
            tabs=create_tabs(definitions=self.tabs),
            description="Offline financial report",
            hide_tabs=True,
        )
        dashboard = self.populate(dashboard, charts)
        self.context.entries = {"chart:" + key: chart for key, chart in charts.items()}
        files = edit_configuration()["dashboard"]["files"]
        files["configs/UI/layout.json"] = copy.deepcopy(self.fixture_layout)
        files["configs/DL objects/dashboard.json"]["description"] = "Offline financial report"
        before = json.dumps(read_config("UI/layout.json"))
        # Exercise an existing overlapping layout without publishing it.
        document = self.backend.documents[dashboard.id]["entry"]
        for tab in document["data"]["tabs"]:
            for position in tab["layout"]:
                if position.get("i") == "month_summary":
                    position["y"] = 8
        dashboard = self.backend.client.get.dashboard(by_id=dashboard.id)
        pulled = pull_ui(
            dashboard,
            files,
            self.datasets,
            {key: {**value, "id": charts[key].id} for key, value in self.definitions.items()},
        )
        for positions in pulled["configs/UI/layout.json"].values():
            assert all(
                type(number) is int for position in positions.values() for number in position
            )
        pulled.pop("chart_titles")
        self.configuration_reads.stop()
        validate_import(pulled, self.context, self.datasets, {"dashboard": dashboard})
        assert json.dumps(read_config("UI/layout.json")) == before

    def test_chart_groups_keep_internal_tabs_filters_and_import_order(self):
        charts = self.build_charts()
        dashboard = create_dashboard(
            context=self.context,
            path=self.backend.folder.key + "Dynamics report",
            tabs=create_tabs(definitions=self.tabs),
            description="Offline financial report",
            hide_tabs=True,
        )
        dashboard = self.populate(dashboard, charts)
        tab = next(tab for tab in dashboard.tabs if tab.id == "dynamics")
        widgets = {item.id: item for item in tab.items}
        assert len(widgets["dynamics_costs"].data["tabs"]) == 5
        assert "dynamics_recipients" not in widgets
        assert (
            self.definitions["dynamics_cost_overview"]["fields"]["y2"]
            == self.definitions["dynamics_recipients"]["fields"]["y"]
        )
        assert (
            widgets["dynamics_costs"].data["tabs"][-1]["chartId"]
            == charts["dynamics_recipients"].id
        )
        assert len(widgets["dynamics_financial"].data["tabs"]) == 4
        definitions = {
            key: {**value, "id": charts[key].id} for key, value in self.definitions.items()
        }
        files = edit_configuration()["dashboard"]["files"]
        files["configs/UI/layout.json"] = copy.deepcopy(self.fixture_layout)
        pulled = pull_ui(dashboard, files, self.datasets, definitions)
        assert pulled["configs/UI/chart_groups.json"] == files["configs/UI/chart_groups.json"]
        assert (
            pulled["configs/UI/links/connections.json"]
            == files["configs/UI/links/connections.json"]
        )
        before = len(self.backend.writes)
        self.populate(dashboard, charts)
        assert len(self.backend.writes) == before

    def test_adding_dynamics_tab_preserves_month_content(self):
        charts = self.build_charts()
        dashboard = create_dashboard(
            context=self.context,
            path=self.backend.folder.key + "Dynamics report",
            tabs=create_tabs(definitions={"month": self.tabs["month"]}),
            description="Offline financial report",
            hide_tabs=True,
        )
        dashboard = populate_dashboard(
            context=self.context,
            dashboard=dashboard,
            datasets=self.datasets,
            charts=charts,
            tab_definitions={"month": self.tabs["month"]},
            contents={"month": self.contents["month"]},
            chart_definitions=self.definitions,
            description="Offline financial report",
            hide_tabs=True,
        )
        document = self.backend.documents[dashboard.id]["entry"]["data"]
        original = copy.deepcopy(next(tab for tab in document["tabs"] if tab["id"] == "month"))
        self.populate(dashboard, charts)
        document = self.backend.documents[dashboard.id]["entry"]["data"]
        assert next(tab for tab in document["tabs"] if tab["id"] == "month") == original

    def test_group_connection_normalization_keeps_every_internal_wire(self):
        tab = SimpleNamespace(
            items=[
                SimpleNamespace(
                    id="costs",
                    item_type="widget",
                    data={"tabs": [{"id": "accrued"}, {"id": "spent"}]},
                )
            ],
            global_items=[],
            controls=[],
            connections=[{"from": "accrued", "to": "date"}, {"from": "spent", "to": "date"}],
        )
        assert normalized_edges(tab, all_routes=True) == {
            ("costs", "date"): [("accrued", "date"), ("spent", "date")]
        }

    def test_group_selectors_move_out_of_filter_groups_with_stable_bindings(self):
        charts = self.build_charts()
        original = copy.deepcopy(self.contents)
        for tab, content in original.items():
            key = tab + "_group_selector"
            selector = content["selectors"].pop(key + "_control")
            group = content["selector_groups"][tab + "_filters"]
            group["members"].insert(2, key)
            group["definitions"][key] = selector
            for kind in (
                "titles",
                "texts",
                "charts",
                "chart_groups",
                "selector_groups",
                "selectors",
            ):
                for value in content.get(kind, {}).values():
                    position = value if isinstance(value, list) else value["at"]
                    if position[1] >= selector["at"][1] + selector["at"][3]:
                        position[1] -= selector["at"][3]
        dashboard = create_dashboard(
            context=self.context,
            path=self.backend.folder.key + "Financial report",
            tabs=create_tabs(definitions=self.tabs),
            description="Offline financial report",
            hide_tabs=True,
        )
        dashboard = populate_dashboard(
            context=self.context,
            dashboard=dashboard,
            datasets=self.datasets,
            charts=charts,
            tab_definitions=self.tabs,
            contents=original,
            chart_definitions=self.definitions,
            description="Offline financial report",
            hide_tabs=True,
        )
        result = self.populate(dashboard, charts)
        for tab in result.tabs:
            key = tab.id + "_group_selector"
            standalone = next(control for control in tab.controls if control.id == key + "_control")
            assert [member.id for member in standalone.members] == [key]
            assert key not in [
                member.id
                for control in tab.controls
                if control.id == tab.id + "_filters"
                for member in control.members
            ]
            assert standalone.members[0].source.dataset_id == self.datasets[tab.id].id
        assert (
            dashboard_issues(
                result,
                tab_definitions=self.tabs,
                contents=self.contents,
                datasets=self.datasets,
                charts=charts,
                chart_definitions=self.definitions,
                description="Offline financial report",
                hide_tabs=True,
            )
            == []
        )
        writes = len(self.backend.writes)
        self.populate(result, charts)
        assert len(self.backend.writes) == writes

    def test_dynamics_defaults_and_parameter_scope(self):
        members = self.contents["dynamics"]["selector_groups"]["dynamics_filters"]["definitions"]
        assert members["dynamics_period_selector"]["control"]["default_value"] == "day"
        assert members["dynamics_conversion_selector"]["control"]["default_value"] == "period"
        assert members["dynamics_money_selector"]["control"]["default_value"] == "total"
        for member in members.values():
            assert all(key.startswith("dynamics_") for key in member["recipients"])
        experiment = members["dynamics_experiment_selector"]["control"]
        assert not experiment["required"]
        assert experiment["multiselect"]
        assert experiment["default_value"] == []
        for key in (
            "dynamics_revenue",
            "dynamics_revenue_net",
            "dynamics_margin",
            "dynamics_margin_pct",
        ):
            assert len(self.definitions[key]["fields"]["y"]) == 2
            assert self.definitions[key]["fields"]["y2"] == (
                ["Доп. РТО"] if key == "dynamics_revenue" else []  # noqa: RUF001 - Russian presentation contract.
            )
        assert len(self.definitions["dynamics_funnel"]["fields"]["y"]) == 3
        calculations = self.dataset_definitions["dynamics"]["calculations"]
        assert "[conversion_mode]" in calculations["Гросс-маржа, %: Тест"]["formula"]
        assert "[conversion_mode]" in calculations["Клиенты с бонусами"]["formula"]  # noqa: RUF001 - Russian presentation contract.
        assert "[РТО без НДС Тест исходное]" in calculations["Гросс-маржа, %: Тест"]["formula"]  # noqa: RUF001 - Russian presentation contract.

    def test_dynamics_alpha_roundtrip_for_chart_group(self):
        charts = self.build_charts()
        dashboard = create_dashboard(
            context=self.context,
            path=self.backend.folder.key + "Financial report",
            tabs=create_tabs(definitions=self.tabs),
            description="Offline financial report",
            hide_tabs=True,
        )
        dashboard = self.populate(dashboard, charts)
        tab = next(tab for tab in dashboard.tabs if tab.id == "dynamics")
        group = next(item for item in tab.controls if item.id == "dynamics_alpha_selector_control")
        alpha = next(member for member in group.members if member.id == "dynamics_alpha_selector")
        assert alpha.source.param_name == "alpha"
        assert alpha.source.element_type == "input"
        assert alpha.source.default_value == "0.01"
        assert (
            dashboard_issues(
                dashboard,
                tab_definitions=self.tabs,
                contents=self.contents,
                datasets=self.datasets,
                charts=charts,
                chart_definitions=self.definitions,
                description="Offline financial report",
                hide_tabs=True,
            )
            == []
        )

    def test_summary_and_detail_use_the_same_parameterized_dataset(self):
        coverage = validate_recipe(
            self.dataset_definitions, self.definitions, self.tabs, self.contents
        )
        assert coverage["object_count"] == 12
        assert set(self.contents) == {"month", "dynamics"}
        for tab, content in self.contents.items():
            if tab != "month":
                continue
            assert set(content["charts"]) == {"month_funnel", "month_summary"}
            for key in content["charts"]:
                assert self.definitions[key]["family"] == "wizard"
                assert self.definitions[key]["tab"] == tab
                assert self.definitions[key]["dataset"] == tab

    def test_manual_selector_requires_the_receiving_dataset_parameter(self):
        original = configuration.read_config

        def changed(name):
            value = copy.deepcopy(original(name))
            if name == "DL objects/datasets.json":
                value["month"]["parameters"] = {}
            return value

        with patch.object(configuration, "read_config", side_effect=changed), pytest.raises(
            DataLensUtilsError, match="not declared"
        ):
            configuration.read_contents()
        assert self.backend.writes == []
