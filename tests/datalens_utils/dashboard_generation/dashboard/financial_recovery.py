# Конфигурация, сериализация и восстановление без облака и доступа к базам.

import json
from unittest.mock import create_autospec

import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.create import (
    create_dashboard,
    create_tabs,
)
from analytics_toolkit.datalens_utils.dashboard_generation.datasets.create import (
    _configure_fields,
    dataset_issues,
    source_query,
)
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.resources.store import ResourceStore
from analytics_toolkit.datalens_utils.validation.charts import check_chart
from analytics_toolkit.datalens_utils.validation.dashboard import (
    actual_alias_groups,
    dashboard_issues,
    normalized_edges,
)
from datalens_sdk import DashboardTab, Dataset

from tests.datalens_utils._support.financial import FinancialCase


class FinancialReportTests(FinancialCase):
    def test_subselect_projections_have_no_statement_terminator(self):
        for definition in self.dataset_definitions.values():
            query = source_query(self.context, definition)
            assert not query.rstrip().endswith(";")
            assert "__TABLE_" not in query

    def test_tables_roundtrip_and_unchanged_rerun_preserves_ids_without_writes(self):
        charts = self.build_charts()
        for key, chart in charts.items():
            assert (
                check_chart(
                    chart,
                    context=self.context,
                    datasets=self.datasets,
                    definition=self.definitions[key],
                )
                == []
            )
        writes = len(self.backend.writes)
        self.context = self.new_context()
        again = self.build_charts()
        assert {key: chart.id for key, chart in charts.items()} == {
            key: chart.id for key, chart in again.items()
        }
        assert len(self.backend.writes) == writes

    def test_missing_chart_period_filter_is_repaired_with_the_same_id(self):
        charts = self.build_charts()
        key = "month_funnel"
        chart = charts[key]
        field = self.datasets["month"].fields.by_name("Выбранный период")
        chart = self.context.resources.persisted(
            "chart:" + key,
            chart.update.delete_filter(field).mode("publish").execute(),
            self.backend.client.get.wizard_chart,
        )
        assert "filters" in check_chart(
            chart, context=self.context, datasets=self.datasets, definition=self.definitions[key]
        )
        repaired = self.build_charts()[key]
        assert repaired.id == chart.id
        assert (
            check_chart(
                repaired,
                context=self.context,
                datasets=self.datasets,
                definition=self.definitions[key],
            )
            == []
        )

    def test_dashboard_filter_defaults_and_layout_roundtrip(self):
        charts = self.build_charts()
        dashboard = create_dashboard(
            context=self.context,
            path=self.backend.folder.key + "Financial report",
            tabs=create_tabs(definitions=self.tabs),
            description="Offline financial report",
            hide_tabs=True,
        )
        dashboard = self.populate(dashboard, charts)
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
        for tab in dashboard.tabs:
            if tab.id != "month":
                continue
            group = next(control for control in tab.controls if control.id == tab.id + "_filters")
            member = next(
                member
                for control in tab.controls
                for member in control.members
                if member.id == tab.id + "_group_selector"
            )
            assert member.source.default_value == ["Все тестовые группы"]  # noqa: RUF001 - Russian presentation contract.
            expected = self.contents["month"]["selectors"]["month_group_selector_control"][
                "control"
            ]
            assert member.source.operation == expected.get("operation")
            assert member.source.multiselect == expected["multiselect"]
            assert member.source.required
            assert len(group.members) == 5
            for key in ("month_start_selector", "month_end_selector"):
                date = next(member for member in group.members if member.id == key)
                assert date.source.element_type == "select"
                assert date.source.operation is None
            assert dashboard.data["settings"]["dependentSelectors"]
            assert frozenset(
                ["report_period", self.datasets["month"].fields.by_name("Тип периода").guid]
            ) in [frozenset(group) for group in tab.aliases["default"]]
            alpha = next(member for member in group.members if member.id == "alpha_selector")
            assert alpha.source.param_name == "alpha"
            assert alpha.source.element_type == "input"
            assert alpha.source.default_value == "0.01"
            period = next(member for member in group.members if member.id == "period_type_selector")
            assert period.source_type == "manual"
            assert period.source.param_name == "report_period"
            assert period.source.default_value == ["month"]
            assert period.source.operation is None
            assert [
                (option["value"], option["title"]) for option in period.source.acceptable_values
            ] == [("month", "Месяц"), ("week", "Неделя"), ("total", "Всё время")]
            wrapper = next(item for item in tab.items if item.id == group.id)
            assert (
                wrapper.data["updateControlsOnChange"]
                == self.contents["month"]["selector_groups"][group.id]["update_on_change"]
            )
            assert not wrapper.data["buttonApply"]
        writes = len(self.backend.writes)
        again = self.populate(dashboard, charts)
        assert again.id == dashboard.id
        assert len(self.backend.writes) == writes

    def test_obsolete_managed_tabs_are_removed_without_replacing_chart_ids(self):
        charts = self.build_charts()
        dashboard = create_dashboard(
            context=self.context,
            path=self.backend.folder.key + "Financial report",
            tabs=create_tabs(definitions=self.tabs),
            description="Offline financial report",
            hide_tabs=True,
        )
        dashboard = self.populate(dashboard, charts)
        update = dashboard.update
        for role in ("week", "total"):
            update.add_tab(
                DashboardTab(role, tab_id=role).add_title(role, item_id=role + "_heading")
            )
        dashboard = self.context.resources.persisted(
            "dashboard", update.mode("publish").execute(), self.backend.client.get.dashboard
        )
        managed = self.context.resources.state["resources"]["dashboard"]["managed_items"]
        self.context.resources.phase(
            "dashboard", "published", managed_items=[*managed, "week_heading", "total_heading"]
        )
        dashboard = self.populate(dashboard, charts)
        assert [tab.id for tab in dashboard.tabs] == ["month", "dynamics"]
        assert {key: chart.id for key, chart in charts.items()} == {
            key: chart.id for key, chart in self.build_charts().items()
        }

    def test_source_role_consolidation_preserves_checkpoint_and_rejects_new_table(self):
        charts = self.build_charts()
        state = json.loads(self.path.read_text())
        state["target"]["source_tables"].update(
            {
                "week": self.context.source_tables["month"],
                "total": self.context.source_tables["month"],
            }
        )
        self.path.write_text(json.dumps(state))
        context = self.new_context()
        assert (
            context.resources.state["resources"]["chart:month_funnel"]["id"]
            == charts["month_funnel"].id
        )
        state["target"]["source_tables"]["month"] = "other.unrelated_table"
        self.path.write_text(json.dumps(state))
        with pytest.raises(DataLensUtilsError, match="another organization, folder, or source"):
            self.new_context()

    def test_adding_dynamics_source_keeps_existing_resource_identity(self):
        charts = self.build_charts()
        state = json.loads(self.path.read_text())
        state["target"]["source_tables"].pop("dynamics")
        self.path.write_text(json.dumps(state))
        context = self.new_context()
        assert (
            context.resources.state["resources"]["chart:month_funnel"]["id"]
            == charts["month_funnel"].id
        )
        state["target"]["source_tables"]["month"] = "other.unrelated_table"
        self.path.write_text(json.dumps(state))
        with pytest.raises(DataLensUtilsError, match="another organization, folder, or source"):
            self.new_context()

    def test_source_switch_requires_matching_owned_dataset_id(self):
        self.context.resources.checkpoint(
            "dataset:dynamics", self.datasets["dynamics"], scope="dataset"
        )
        state = json.loads(self.path.read_text())
        state["target"]["source_tables"]["dynamics"] = "pa_core_marts.stage_preview"
        self.path.write_text(json.dumps(state))
        target = {**state["target"], "source_tables": self.context.source_tables}
        store = ResourceStore(
            self.backend.folder,
            target,
            path=self.path,
            dataset_ids={"dynamics": self.datasets["dynamics"].id},
        )
        assert store.state["resources"]["dataset:dynamics"]["id"] == self.datasets["dynamics"].id
        with pytest.raises(DataLensUtilsError, match="another organization, folder, or source"):
            ResourceStore(
                self.backend.folder,
                target,
                path=self.path,
                dataset_ids={"dynamics": "unrelated-dataset"},
            )

    def test_failed_fetch_recovers_committed_chart_without_duplicate_creation(self):
        key = "month_summary"
        definition = {key: self.definitions[key]}
        self.backend.fail_after_operation = "createWizardChart"
        with pytest.raises(RuntimeError, match="re-fetch"):
            self.build_charts(definition)
        saved = json.loads(self.path.read_text())["resources"]["chart:" + key]["id"]
        self.context = self.new_context()
        chart = self.build_charts(definition)[key]
        assert chart.id == saved
        assert sum(name == "createWizardChart" for name, _ in self.backend.writes) == 1

    def test_period_reaches_dates_with_no_reverse_influence(self):
        charts = self.build_charts()
        dashboard = create_dashboard(
            context=self.context,
            path=self.backend.folder.key + "Financial report",
            tabs=create_tabs(definitions=self.tabs),
            description="Offline financial report",
            hide_tabs=True,
        )
        dashboard = self.populate(dashboard, charts)
        edges = normalized_edges(dashboard.tabs[0])
        for date in ("month_start_selector", "month_end_selector"):
            assert (date, "period_type_selector") not in edges
            assert ("period_type_selector", date) in edges
            assert (date, "alpha_selector") in edges
        for chart in ("month_funnel", "month_summary"):
            for selector in (
                "period_type_selector",
                "month_start_selector",
                "month_end_selector",
                "month_experiment_selector",
                "month_group_selector",
                "alpha_selector",
            ):
                assert (chart, selector) not in edges

    def test_legacy_parameter_alias_migrates_without_removing_user_aliases(self):
        charts = self.build_charts()
        dashboard = create_dashboard(
            context=self.context,
            path=self.backend.folder.key + "Financial report",
            tabs=create_tabs(definitions=self.tabs),
            description="Offline financial report",
            hide_tabs=True,
        )
        legacy = frozenset(
            ("report_period", self.datasets["month"].fields.by_name("report_period").guid)
        )
        user_alias = frozenset(
            (self.datasets["month"].fields.by_name("Начало периода").guid, "user-date-field")
        )
        dashboard = self.context.resources.persisted(
            "dashboard",
            dashboard.update.add_alias(*sorted(legacy), tab="month")
            .add_alias(*sorted(user_alias), tab="month")
            .mode("save")
            .execute(),
            self.backend.client.get.dashboard,
            branch="saved",
        )
        dashboard = self.populate(dashboard, charts)
        aliases = actual_alias_groups(dashboard.tabs[0])
        assert legacy not in aliases
        assert user_alias in aliases
        assert (
            frozenset(("report_period", self.datasets["month"].fields.by_name("Тип периода").guid))
            in aliases
        )
        writes = len(self.backend.writes)
        self.context = self.new_context()
        again = self.populate(dashboard, charts)
        assert actual_alias_groups(again.tabs[0]) == aliases
        assert len(self.backend.writes) == writes

    def test_summary_preserves_period_granularity_and_filters_raw_arpu(self):
        dataset = self.datasets["month"]
        chart = self.build_charts()["month_summary"]
        definition = self.definitions["month_summary"]
        dimensions = [
            name
            for name in definition["fields"]["columns"]
            if dataset.fields.by_name(name).type == "DIMENSION"
        ]
        assert dimensions == ["Период сводки"]
        raw_arpu = dataset.fields.by_name("ARPU ЦА, руб. (исходное)")  # noqa: RUF001 - Russian presentation contract.
        assert raw_arpu.guid in [rule["guid"] for rule in chart.data["sources"]["filters"]]
        assert dataset.fields.by_name("ARPU ЦА, руб.").guid not in [  # noqa: RUF001 - Russian presentation contract.
            rule["guid"] for rule in chart.data["sources"]["filters"]
        ]
        assert dataset.fields.by_name("Сводка: Начало периода").cast == "date"
        assert dataset.fields.by_name("Сводка: Конец периода").cast == "date"

    def test_new_projection_column_is_added_without_replacing_existing_fields(self):
        configured = self.datasets["month"]
        seeded = self.backend.seed_dataset("month")
        dataset = Dataset(
            id=configured.id,
            name=configured.name,
            description=configured.description,
            installation=configured.installation,
            sources=seeded.sources,
            source_avatars=seeded.source_avatars,
            result_schema=tuple(
                field.raw for field in configured.fields if field.source != "summary_period_label"
            ),
        )
        assert dataset_issues(dataset, self.dataset_definitions["month"]) == [
            "summary_period_label missing"
        ]
        builder = create_autospec(type(dataset.update), instance=True)
        _configure_fields(builder, dataset, self.dataset_definitions["month"])
        builder.add_field.assert_called_once_with(
            title="Период сводки",
            source="summary_period_label",
            kind="DIMENSION",
            aggregation="none",
            cast="string",
            avatar_id=seeded.source_avatars[0]["id"],
        )
        builder.update_field.assert_not_called()
        builder.change_field_aggregation.assert_not_called()
        builder.update_calculation.assert_not_called()
