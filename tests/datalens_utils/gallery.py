"""Gallery creation and recovery through the installed public SDK."""

import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.charts.create import create_charts
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.configuration import (
    read_contents,
)
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.create import (
    create_dashboard,
    create_tabs,
)
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.populate import (
    add_item,
    populate_dashboard,
)
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.resources.store import ResourceStore
from analytics_toolkit.datalens_utils.settings import (
    read_chart_definitions,
    read_config,
    source_tables,
)
from analytics_toolkit.datalens_utils.validation.charts import check_chart
from analytics_toolkit.datalens_utils.validation.dashboard import (
    dashboard_issues,
    definition_items,
    expected_edges,
    managed_edges,
    selector_bindings,
)

from tests.datalens_utils._support.context import ProjectTestCase
from tests.datalens_utils._support.sdk import MemoryDataLens, configured_datasets


class GalleryTests(ProjectTestCase):
    def setUp(self):
        self.backend = MemoryDataLens()
        self.addCleanup(self.backend.client.close)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "resources.json"
        self.definitions = read_chart_definitions()
        self.dataset_definitions = read_config("DL objects/datasets.json")
        self.datasets = configured_datasets(self.dataset_definitions)
        self.context = self.new_context()

    def new_context(self):
        resources = ResourceStore(
            self.backend.folder,
            {
                "folder_path": self.backend.folder.key.rstrip("/"),
                "organization_id": "offline",
                "connection_id": self.backend.connection.id,
                "tables": source_tables(self.dataset_definitions),
            },
            path=self.path,
        )
        resources.set_folders(
            {
                "dash": self.backend.folder,
                "widget": self.backend.chart_folder,
                "dataset": self.backend.folder,
            }
        )
        return SimpleNamespace(
            client=self.backend.client,
            folder=self.backend.folder,
            connection=self.backend.connection,
            resources=resources,
            source_tables=source_tables(self.dataset_definitions),
        )

    def build_charts(self, definitions=None):
        return create_charts(
            context=self.context,
            datasets=self.datasets,
            definitions=self.definitions if definitions is None else definitions,
        )

    def native_selector_members(self):
        """Serialize and re-fetch the native controls through public SDK calls."""
        contents = read_contents()
        tabs = create_tabs(definitions=read_config("UI/tabs.json"))
        builder = self.backend.client.create.dashboard(
            name="Native selector defaults", location=self.backend.folder
        )
        for key in ("wizard", "ql"):
            content = {kind: contents[key][kind] for kind in ("selectors", "selector_groups")}
            for item_id, definition in definition_items(content, {}, {}).items():
                add_item(
                    tabs[key],
                    tab=None,
                    item_id=item_id,
                    definition=definition,
                    datasets=self.datasets,
                )
            builder.add_tab(tabs[key])
        created = builder.build()
        dashboard = self.backend.client.get.dashboard(by_id=created.id, branch="published")
        return {
            member.id: member
            for tab in dashboard.tabs
            for control in tab.controls
            for member in control.members
        }

    def test_native_date_range_defaults_preserve_full_utc_boundaries(self):
        members = self.native_selector_members()
        interval = "__interval_2026-07-01T00:00:00.000Z_2026-09-30T23:59:59.999Z"
        for key in ("wizard_interval_selector", "ql_interval_selector"):
            with self.subTest(selector=key):
                assert members[key].source.default_value == interval
                assert members[key].source.raw["defaultValue"] == interval
        field = self.datasets["retail"].fields.by_name("Date").guid
        assert members["wizard_interval_selector"].raw["defaults"][field] == "__between_" + interval
        assert members["ql_interval_selector"].raw["defaults"]["interval"] == interval

    def test_blank_native_text_input_has_no_literal_filter_operation_as_value(self):
        member = self.native_selector_members()["wizard_search_selector"]
        field = self.datasets["retail"].fields.by_name("Category").guid
        assert member.source.operation == "ICONTAINS"
        assert member.raw["defaults"][field] == ""
        assert "defaultValue" not in member.source.raw
        assert "__icontains_" not in json.dumps(member.raw)

    def test_every_gallery_chart_serializes_refetches_and_unchanged_rerun_is_noop(self):
        coverage = read_config("coverage.json")
        expected = {family: value["count"] for family, value in coverage["families"].items()}
        expected["editor"] += coverage["external_selector_count"]
        assert Counter(d["family"] for d in self.definitions.values()) == expected
        charts = self.build_charts()
        assert len(charts) == coverage["object_count"]
        for role, chart in charts.items():
            with self.subTest(chart=role):
                assert (
                    check_chart(
                        chart,
                        context=self.context,
                        datasets=self.datasets,
                        definition=self.definitions[role],
                    )
                    == []
                )
                assert chart.key.rpartition("/")[0] == self.backend.chart_folder.key.rstrip("/")
        creations = len(self.backend.writes)
        assert creations == coverage["object_count"]
        self.context = self.new_context()
        again = self.build_charts()
        assert {key: chart.id for key, chart in charts.items()} == {
            key: chart.id for key, chart in again.items()
        }
        assert len(self.backend.writes) == creations

    def test_invalid_last_definition_prevents_all_remote_writes(self):
        definitions = copy.deepcopy(self.definitions)
        final = next(reversed(definitions))
        definitions[final]["type"] = "not_an_installed_sdk_factory"
        with pytest.raises(DataLensUtilsError):
            self.build_charts(definitions)
        assert self.backend.writes == []
        assert self.backend.documents == {}

    def test_persisted_creation_checkpoint_recovers_fetch_failure_each_family(self):
        for family, operation in (
            ("wizard", "createWizardChart"),
            ("ql", "createQLChart"),
            ("editor", "createEditorChart"),
        ):
            role = next(
                key
                for key, definition in self.definitions.items()
                if definition["family"] == family
            )
            definition = {role: self.definitions[role]}
            with self.subTest(family=family):
                self.backend.fail_after_operation = operation
                with pytest.raises(RuntimeError, match="re-fetch"):
                    self.build_charts(definition)
                checkpoint = json.loads(self.path.read_text(encoding="utf-8"))["resources"][
                    f"chart:{role}"
                ]["id"]
                self.context = self.new_context()
                chart = self.build_charts(definition)[role]
                assert chart.id == checkpoint
                assert sum(name == operation for name, _ in self.backend.writes) == 1

    def test_missing_checkpoint_adopts_exact_names_without_creating_duplicates(self):
        roles = [
            next(key for key, d in self.definitions.items() if d["family"] == family)
            for family in ("wizard", "ql", "editor")
        ]
        definitions = {role: self.definitions[role] for role in roles}
        charts = self.build_charts(definitions)
        self.path.unlink()
        writes = len(self.backend.writes)
        self.context = self.new_context()
        adopted = self.build_charts(definitions)
        assert {role: chart.id for role, chart in adopted.items()} == {
            role: chart.id for role, chart in charts.items()
        }
        assert len(self.backend.writes) == writes

    def test_map_topology_change_is_rejected_before_an_earlier_chart_update(self):
        definitions = {
            key: definition
            for key, definition in self.definitions.items()
            if key in {"wizard_line", "wizard_map_points"}
        }
        charts = self.build_charts(definitions)
        changed = copy.deepcopy(definitions)
        changed["wizard_line"]["title"] += " renamed"
        changed["wizard_map_points"]["map_center"]["zoom"] += 1
        writes = len(self.backend.writes)
        with pytest.raises(DataLensUtilsError, match="topology"):
            self.build_charts(changed)
        assert len(self.backend.writes) == writes
        assert (
            charts["wizard_line"].id
            == self.context.resources.state["resources"]["chart:wizard_line"]["id"]
        )

    def test_wizard_title_style_update_preserves_local_formulas_and_identity(self):
        definitions = {"wizard_line": self.definitions["wizard_line"]}
        original = self.build_charts(definitions)["wizard_line"]
        locals_before = {
            field.guid: field.formula for field in original.fields if field.calc_mode == "formula"
        }
        changed = copy.deepcopy(definitions)
        changed["wizard_line"]["title"] = "Updated revenue trend"
        changed["wizard_line"]["formats"]["Revenue"]["precision"] = 1
        writes = len(self.backend.writes)
        updated = self.build_charts(changed)["wizard_line"]
        assert updated.id == original.id
        assert {
            field.guid: field.formula for field in updated.fields if field.calc_mode == "formula"
        } == locals_before
        assert [name for name, _ in self.backend.writes[writes:]] == ["updateWizardChart"]
        assert (
            check_chart(
                updated,
                context=self.context,
                datasets=self.datasets,
                definition=changed["wizard_line"],
            )
            == []
        )

    def test_wizard_structural_rebuild_resumes_saved_draft_without_recreation(self):
        definitions = {"wizard_line": self.definitions["wizard_line"]}
        original = self.build_charts(definitions)["wizard_line"]
        changed = copy.deepcopy(definitions)
        changed["wizard_line"]["sort"] = [["Date", "desc"]]
        self.backend.fail_after_operation = "updateWizardChart"
        with pytest.raises(RuntimeError, match="re-fetch"):
            self.build_charts(changed)
        saved = self.backend.client.get.wizard_chart(by_id=original.id, branch="saved")
        assert saved.saved_id != saved.published_id
        self.context = self.new_context()
        updated = self.build_charts(changed)["wizard_line"]
        assert updated.id == original.id
        assert updated.saved_id == updated.published_id
        assert sum(operation == "createWizardChart" for operation, _ in self.backend.writes) == 1
        assert (
            check_chart(
                updated,
                context=self.context,
                datasets=self.datasets,
                definition=changed["wizard_line"],
            )
            == []
        )

    def test_wizard_local_formula_edit_reuses_same_guid(self):
        definitions = {"wizard_line": self.definitions["wizard_line"]}
        original = self.build_charts(definitions)["wizard_line"]
        changed = copy.deepcopy(definitions)
        local = changed["wizard_line"]["local_fields"][0]
        local["formula"] = "MAVG(SUM([Revenue Raw]), 3 TOTAL ORDER BY [Date])"
        updated = self.build_charts(changed)["wizard_line"]
        assert updated.id == original.id
        assert updated.fields.by_guid(local["guid"]).formula == local["formula"]
        assert sum(operation == "createWizardChart" for operation, _ in self.backend.writes) == 1

    def test_ql_and_editor_updates_keep_identity_and_rerun_without_writes(self):
        roles = [
            next(
                key
                for key, definition in self.definitions.items()
                if definition["family"] == family
            )
            for family in ("ql", "editor")
        ]
        definitions = {role: self.definitions[role] for role in roles}
        original = self.build_charts(definitions)
        changed = copy.deepcopy(definitions)
        for definition in changed.values():
            definition["description"] = "Updated company gallery guidance"
        updated = self.build_charts(changed)
        assert {role: chart.id for role, chart in original.items()} == {
            role: chart.id for role, chart in updated.items()
        }
        writes = len(self.backend.writes)
        self.context = self.new_context()
        self.build_charts(changed)
        assert len(self.backend.writes) == writes

    def test_three_tabs_all_selectors_aliases_and_ignore_direction_roundtrip(self):
        charts = self.build_charts()
        tabs, contents = read_config("UI/tabs.json"), read_contents()
        dashboard = create_dashboard(
            context=self.context,
            path=self.backend.folder.key + "Gallery",
            tabs=create_tabs(definitions=tabs),
            description="Offline gallery",
            hide_tabs=False,
        )
        dashboard = populate_dashboard(
            context=self.context,
            dashboard=dashboard,
            datasets=self.datasets,
            charts=charts,
            tab_definitions=tabs,
            contents=contents,
            chart_definitions=self.definitions,
            description="Offline gallery",
            hide_tabs=False,
        )
        assert (
            dashboard_issues(
                dashboard,
                tab_definitions=tabs,
                contents=contents,
                datasets=self.datasets,
                charts=charts,
                chart_definitions=self.definitions,
                description="Offline gallery",
                hide_tabs=False,
            )
            == []
        )
        assert {tab.id for tab in dashboard.tabs if not tab.hidden} == {"wizard", "ql", "editor"}
        for tab in dashboard.tabs:
            bindings = selector_bindings(contents[tab.id])
            assert set(managed_edges(tab, contents[tab.id])) == expected_edges(contents[tab.id])
            # SDK connections express ignored influence: receiver -> selector.
            for receiver, selector in expected_edges(contents[tab.id]):
                assert receiver in contents[tab.id]["charts"]
                assert receiver not in bindings[selector]
        writes = len(self.backend.writes)
        again = populate_dashboard(
            context=self.context,
            dashboard=dashboard,
            datasets=self.datasets,
            charts=charts,
            tab_definitions=tabs,
            contents=contents,
            chart_definitions=self.definitions,
            description="Offline gallery",
            hide_tabs=False,
        )
        assert again.id == dashboard.id
        assert len(self.backend.writes) == writes


if __name__ == "__main__":
    unittest.main()
