import contextlib
import copy
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.configuration import (
    read_contents,
)
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.create import (
    create_dashboard,
    create_tabs,
)
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.populate import (
    populate_dashboard,
)
from analytics_toolkit.datalens_utils.editing import commands
from analytics_toolkit.datalens_utils.editing.cli import parser
from analytics_toolkit.datalens_utils.editing.pull import pull_chart, pull_ui
from analytics_toolkit.datalens_utils.editing.state import (
    EditState,
    configuration,
    fingerprint,
    inventory,
    merge,
)
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.settings import read_config
from analytics_toolkit.datalens_utils.validation.charts import check_chart
from analytics_toolkit.datalens_utils.validation.dashboard import normalize_params

from tests.datalens_utils import gallery as test_gallery
from tests.datalens_utils._support.context import ProjectTestCase


class EditingTests(ProjectTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name) / "edit-state.json"

    def test_cli_keeps_full_default_and_explicit_scope(self):
        assert parser().parse_args([]).command is None
        assert parser().parse_args(["apply", "--chart", "sales"]).chart == ["sales"]
        assert parser().parse_args(["pull", "--branch", "saved"]).branch == "saved"
        with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
            parser().parse_args(["apply", "--chart", "sales", "--ui"])

    def test_inventory_uses_one_batched_call(self):
        entries = [SimpleNamespace(id=str(index)) for index in range(150)]
        client = SimpleNamespace(
            navigation=SimpleNamespace(get_entries=Mock(return_value=iter(entries)))
        )
        units = {f"chart:{index}": {"id": str(index)} for index in range(150)}
        result = inventory(client, units)
        assert len(result) == 150
        client.navigation.get_entries.assert_called_once_with(
            ids=[str(index) for index in range(150)], page_size=100
        )

    def test_three_way_merge_preserves_nonconflicting_changes(self):
        assert merge(
            {"precision": 1, "title": "old"},
            {"precision": 2, "title": "old"},
            {"precision": 1, "title": "new"},
        ) == {"precision": 2, "title": "new"}
        assert merge({"removed": 1}, {}, {"removed": 1}) == {}
        with pytest.raises(DataLensUtilsError, match="conflict"):
            merge({"columns": ["a", "b"]}, {"columns": ["b", "a"]}, {"columns": ["a"]})

    def test_revisions_paths_incomplete_metadata_and_saved_drafts(self):
        units = {
            "chart:one": {
                "id": "one",
                "files": {"file": "old"},
                "fingerprint": fingerprint({"file": "old"}),
            }
        }
        entry = SimpleNamespace(id="one", saved_id="rev1", published_id="rev1", key="folder/chart")
        state = EditState({"target": "test"}, path=self.cache)
        state.remember("chart:one", units["chart:one"], entry)
        assert state.changes(units, {"chart:one": entry})["chart:one"] == "unchanged"
        entry.key = "moved/chart"
        assert state.changes(units, {"chart:one": entry})["chart:one"] == "remote"
        entry.key, entry.saved_id = "folder/chart", "draft"
        state.remember("chart:one", units["chart:one"], entry, branch="saved")
        assert state.changes(units, {"chart:one": entry})["chart:one"] == "local"
        entry.saved_id = "another-draft"
        assert state.changes(units, {"chart:one": entry})["chart:one"] == "both"
        entry.published_id = None
        state.value["resources"]["chart:one"]["branch"] = "published"
        assert state.changes(units, {"chart:one": entry})["chart:one"] == "remote"

    def test_shared_asset_changes_affect_every_consumer(self):
        unit = {"files": {"chart.json": {"fields": {}}, "shared.js": "before"}}
        changed = copy.deepcopy(unit)
        changed["files"]["shared.js"] = "after"
        assert commands.semantic_change(changed, unit)
        changed = copy.deepcopy(unit)
        changed["files"]["chart.json"]["title"] = "new"
        assert not commands.semantic_change(changed, unit)

    def test_recovery_receipts_require_exact_revisions_path_and_recipe(self):
        key = "chart:one"
        unit = {"id": "one", "files": {"file": "new"}, "fingerprint": fingerprint({"file": "new"})}
        entry = SimpleNamespace(id="one", saved_id="rev1", published_id="rev1", key="Offline/one")
        state = EditState({"target": "test"}, path=self.cache)
        state.begin_apply({key: unit}, {key: entry}, {key})
        draft = SimpleNamespace(id="one", saved_id="rev2", published_id="rev1", key="Offline/one")
        state.record_write(key, draft)
        restored = EditState({"target": "test"}, path=self.cache)
        assert restored.owns_write(key, unit, draft)
        for attribute, value in (
            ("id", "other"),
            ("saved_id", "external-draft"),
            ("published_id", "external-publication"),
            ("key", "Moved/one"),
        ):
            changed = copy.copy(draft)
            setattr(changed, attribute, value)
            assert not restored.owns_write(key, unit, changed)
        assert not restored.owns_write(key, {**unit, "fingerprint": "changed"}, draft)
        restored.remember(key, unit, entry)
        assert not restored.owns_write(key, unit, draft)

    def test_noop_apply_reads_inventory_only(self):
        units = configuration()
        entries = {
            key: SimpleNamespace(
                id=unit["id"], saved_id="revision", published_id="revision", key="folder/" + key
            )
            for key, unit in units.items()
        }
        deployment = {
            "organization_id": "offline",
            "target_path": "folder",
            "dashboard_name": "Dashboard",
            "connection_id": "offline",
            "connection_name": "offline",
        }
        state = EditState(commands.deployment_target(deployment), path=self.cache)
        for key, unit in units.items():
            state.remember(key, unit, entries[key])
        state.save()
        client = Mock()
        with patch.object(commands, "EditState", return_value=state), patch.object(
            commands, "datalens_client"
        ) as provider, patch.object(
            commands, "inventory", return_value=entries
        ) as listing, patch.object(commands, "context_for") as context, contextlib.redirect_stdout(
            io.StringIO()
        ):
            provider.return_value.__enter__.return_value = client
            commands.run(parser().parse_args(["apply"]), deployment)
        listing.assert_called_once()
        context.assert_not_called()
        assert client.mock_calls == []


class EditingAdapterTests(ProjectTestCase):
    def setUp(self):
        self.gallery = test_gallery.GalleryTests()
        self.gallery.setUp()
        self.addCleanup(self.gallery.doCleanups)
        self.charts = self.gallery.build_charts()

    def test_all_chart_families_pull_to_matching_typed_recipes(self):
        for role, chart in self.charts.items():
            with self.subTest(chart=role):
                definition, _ = pull_chart(
                    chart,
                    self.gallery.definitions[role],
                    self.gallery.datasets,
                    self.gallery.context,
                )
                assert (
                    check_chart(
                        chart,
                        context=self.gallery.context,
                        datasets=self.gallery.datasets,
                        definition=definition,
                    )
                    == []
                )

    def test_table_user_order_titles_formats_sort_and_filters_are_imported(self):
        chart = self.charts["wizard_flat_table"]
        dataset = self.gallery.datasets["retail"]
        chart = (
            chart.update.columns(
                [dataset.fields.by_name("Revenue"), dataset.fields.by_name("Region")]
            )
            .column_title(dataset.fields.by_name("Revenue"), title="Выручка")
            .measure_format(dataset.fields.by_name("Revenue"), precision=3)
            .add_filter(dataset.fields.by_name("Revenue"), operation="GT", values=["0"])
            .mode("publish")
            .execute()
        )
        chart = self.gallery.backend.client.get.wizard_chart(by_id=chart.id, branch="published")
        definition, _ = pull_chart(
            chart,
            self.gallery.definitions["wizard_flat_table"],
            self.gallery.datasets,
            self.gallery.context,
        )
        assert definition["fields"]["columns"] == ["Revenue", "Region"]
        assert definition["formats"]["Revenue"]["precision"] == 3
        assert {"field": "Revenue", "title": "Выручка"} in definition["settings"]["column_title"]
        assert definition["filters"][0]["operation"] == "GT"

    def test_unmodified_ql_keeps_template_and_editor_sources_are_exact(self):
        _, assets = pull_chart(
            self.charts["ql_flat_table"],
            self.gallery.definitions["ql_flat_table"],
            self.gallery.datasets,
            self.gallery.context,
        )
        assert assets == {}
        definition = self.gallery.definitions["js_table"]
        _, assets = pull_chart(
            self.charts["js_table"], definition, self.gallery.datasets, self.gallery.context
        )
        for tab, relative in definition["scripts"].items():
            assert assets[relative] == self.charts["js_table"].data[tab]

    def build_dashboard(self):
        definitions = read_config("UI/tabs.json")
        dashboard = create_dashboard(
            context=self.gallery.context,
            path=self.gallery.backend.folder.key + "Editing",
            tabs=create_tabs(definitions=definitions),
            description="",
            hide_tabs=False,
        )
        contents = read_contents()
        dashboard = populate_dashboard(
            context=self.gallery.context,
            dashboard=dashboard,
            datasets=self.gallery.datasets,
            charts=self.charts,
            tab_definitions=definitions,
            contents=contents,
            chart_definitions=self.gallery.definitions,
            description="",
            hide_tabs=False,
        )
        return dashboard, definitions, contents

    def test_layout_change_validates_saved_revision_and_preserves_item_ids_and_wiring(self):
        dashboard, tabs, contents = self.build_dashboard()
        before = len(self.gallery.backend.writes)
        old_connections = [(tab.id, tab.connections) for tab in dashboard.tabs]
        contents = copy.deepcopy(contents)
        contents["wizard"]["charts"]["wizard_flat_table"][2] -= 1
        again = populate_dashboard(
            context=self.gallery.context,
            dashboard=dashboard,
            datasets=self.gallery.datasets,
            charts=self.charts,
            tab_definitions=tabs,
            contents=contents,
            chart_definitions=self.gallery.definitions,
            description="",
            hide_tabs=False,
        )
        writes = self.gallery.backend.writes[before:]
        assert [name for name, _ in writes] == ["updateDashboard", "updateDashboard"]
        assert writes[0][1]["mode"] == "save"
        assert [(tab.id, tab.connections) for tab in again.tabs] == old_connections

    def test_invalid_reflow_is_saved_without_publishing(self):
        dashboard, tabs, contents = self.build_dashboard()
        before = len(self.gallery.backend.writes)
        contents = copy.deepcopy(contents)
        contents["wizard"]["charts"]["wizard_flat_table"][3] -= 1
        with pytest.raises(DataLensUtilsError, match="Saved dashboard"):
            populate_dashboard(
                context=self.gallery.context,
                dashboard=dashboard,
                datasets=self.gallery.datasets,
                charts=self.charts,
                tab_definitions=tabs,
                contents=contents,
                chart_definitions=self.gallery.definitions,
                description="",
                hide_tabs=False,
            )
        writes = self.gallery.backend.writes[before:]
        assert [name for name, _ in writes] == ["updateDashboard"]
        assert writes[0][1]["mode"] == "save"

    def test_ui_import_validates_without_touching_original_files(self):
        dashboard, _, _ = self.build_dashboard()
        files = configuration()["dashboard"]["files"]
        # Fixture IDs are supplied by the actual synthetic deployment.
        definitions = copy.deepcopy(self.gallery.definitions)
        for role, chart in self.charts.items():
            definitions[role]["id"] = chart.id
        pulled = pull_ui(dashboard, files, self.gallery.datasets, definitions)
        assert pulled["configs/UI/layout.json"] == files["configs/UI/layout.json"]
        assert pulled["chart_titles"] == files["chart_titles"]
        params_path = "configs/UI/links/chart_params.json"
        assert set(pulled[params_path]) == set(files[params_path])
        for key, values in files[params_path].items():
            assert normalize_params(pulled[params_path][key]) == normalize_params(values)
        context = SimpleNamespace(
            **vars(self.gallery.context),
            entries={"chart:" + role: chart for role, chart in self.charts.items()},
        )
        commands.validate_import(
            {key: value for key, value in pulled.items() if key != "chart_titles"},
            context,
            self.gallery.datasets,
            {"dashboard": dashboard},
        )

    def test_ui_pull_imports_changed_and_cleared_widget_parameters(self):
        dashboard, _, _ = self.build_dashboard()
        dashboard = (
            dashboard.update.set_chart_params(
                item_id="ql_column", params={"channel": ["Online"]}, merge=False
            )
            .set_chart_params(item_id="ql_flat_table", params={}, merge=False)
            .mode("publish")
            .execute()
        )
        definitions = copy.deepcopy(self.gallery.definitions)
        for role, chart in self.charts.items():
            definitions[role]["id"] = chart.id
        pulled = pull_ui(
            dashboard, configuration()["dashboard"]["files"], self.gallery.datasets, definitions
        )
        params = pulled["configs/UI/links/chart_params.json"]
        assert params["ql_column"] == {"channel": ["Online"]}
        assert params["ql_flat_table"] == {}
        assert "ql" not in params

    def test_pull_merges_local_precision_with_remote_title_before_file_replacement(self):
        key = "chart:wizard_flat_table"
        units = configuration()
        state = EditState(
            {"target": "offline"}, path=Path(self.gallery.temporary.name) / "editing.json"
        )
        for role, chart in self.charts.items():
            state.remember("chart:" + role, units["chart:" + role], chart)
        path = next(name for name in units[key]["files"] if name.endswith(".json"))
        units[key] = copy.deepcopy(units[key])
        units[key]["files"][path]["formats"]["Revenue"]["precision"] = 3
        units[key]["fingerprint"] = fingerprint(units[key]["files"])
        remote = (
            self.charts["wizard_flat_table"]
            .update.column_title(
                self.gallery.datasets["retail"].fields.by_name("Revenue"), title="Remote title"
            )
            .mode("publish")
            .execute()
        )
        context = SimpleNamespace(
            **vars(self.gallery.context),
            entries={"chart:" + role: chart for role, chart in self.charts.items()},
        )

        def fetch(client, resource, unit, definitions, remote=remote, **options):
            return (
                self.gallery.datasets[resource.split(":", 1)[1]]
                if resource.startswith("dataset:")
                else remote
            )

        with patch.object(commands, "fetch", side_effect=fetch), patch.object(
            commands, "write_files"
        ) as write, patch.object(
            commands, "configuration", return_value=units
        ), contextlib.redirect_stdout(io.StringIO()):
            commands.import_changes(
                context,
                parser().parse_args(["pull", "--chart", "wizard_flat_table"]),
                {key},
                units,
                state,
                self.gallery.dataset_definitions,
                self.gallery.definitions,
            )
        merged = write.call_args.args[0][path]
        assert merged["formats"]["Revenue"]["precision"] == 3
        assert {"field": "Revenue", "title": "Remote title"} in merged["settings"]["column_title"]
        assert state.value["resources"][key]["files"][path]["formats"]["Revenue"]["precision"] == 0

    def test_pull_conflict_and_unsupported_remote_settings_do_not_replace_files(self):
        key = "chart:wizard_flat_table"
        for unsupported in (False, True):
            with self.subTest(unsupported=unsupported):
                units = configuration()
                state = EditState(
                    {"target": "offline"}, path=Path(self.gallery.temporary.name) / "editing.json"
                )
                state.remember(key, units[key], self.charts["wizard_flat_table"])
                path = next(name for name in units[key]["files"] if name.endswith(".json"))
                if not unsupported:
                    units[key] = copy.deepcopy(units[key])
                    units[key]["files"][path]["formats"]["Revenue"]["precision"] = 3
                remote = (
                    self.charts["wizard_flat_table"]
                    .update.measure_format(
                        self.gallery.datasets["retail"].fields.by_name("Revenue"), precision=4
                    )
                    .mode("publish")
                    .execute()
                )
                if unsupported:
                    document = self.gallery.backend.documents[remote.id]["entry"]
                    columns = document["data"]["visualization"]["columns"]["items"]
                    revenue = next(
                        value
                        for value in columns
                        if value["guid"]
                        == self.gallery.datasets["retail"].fields.by_name("Revenue").guid
                    )
                    revenue["formatting"]["unsupportedFormat"] = True
                    remote = self.gallery.backend.client.get.wizard_chart(by_id=remote.id)

                def fetch(client, resource, unit, definitions, remote=remote, **options):
                    return (
                        self.gallery.datasets[resource.split(":", 1)[1]]
                        if resource.startswith("dataset:")
                        else remote
                    )

                with patch.object(commands, "fetch", side_effect=fetch), patch.object(
                    commands, "write_files"
                ) as write, pytest.raises(DataLensUtilsError):
                    commands.import_changes(
                        self.gallery.context,
                        parser().parse_args(["pull", "--chart", "wizard_flat_table"]),
                        {key},
                        units,
                        state,
                        self.gallery.dataset_definitions,
                        self.gallery.definitions,
                    )
                write.assert_not_called()

    def test_remote_or_unimported_draft_blocks_before_any_write(self):
        unit = configuration()["chart:wizard_flat_table"]
        entry = self.charts["wizard_flat_table"]
        state = EditState(
            {"target": "offline"}, path=Path(self.gallery.temporary.name) / "editing.json"
        )
        state.remember("chart:wizard_flat_table", unit, entry)
        before = len(self.gallery.backend.writes)
        with pytest.raises(DataLensUtilsError, match="Pull"):
            commands.apply_changes(
                self.gallery.backend.client,
                {},
                {"chart:wizard_flat_table": entry},
                {"chart:wizard_flat_table"},
                {"chart:wizard_flat_table": "remote"},
                {"chart:wizard_flat_table": unit},
                state,
                {},
                self.gallery.definitions,
                {},
            )
        assert len(self.gallery.backend.writes) == before

    def test_single_chart_format_update_does_not_reconcile_other_charts(self):
        dashboard, _, _ = self.build_dashboard()
        baseline = configuration()
        state = EditState(
            {"target": "offline"}, path=Path(self.gallery.temporary.name) / "editing.json"
        )
        entries = {
            "chart:" + role: SimpleNamespace(
                id=chart.id,
                saved_id=chart.saved_id,
                published_id=chart.published_id,
                key=chart.key,
                is_locked=False,
            )
            for role, chart in self.charts.items()
        }
        entries["dashboard"] = SimpleNamespace(
            id=dashboard.id,
            saved_id=dashboard.saved_id,
            published_id=dashboard.published_id,
            key=dashboard.key,
            is_locked=False,
        )
        for key, entry in entries.items():
            state.remember(key, baseline[key], entry)
        units = copy.deepcopy(baseline)
        key = "chart:wizard_flat_table"
        definition_path = next(name for name in units[key]["files"] if name.endswith(".json"))
        units[key]["files"][definition_path]["formats"]["Revenue"]["precision"] = 3
        units[key]["fingerprint"] = fingerprint(units[key]["files"])
        definitions = copy.deepcopy(self.gallery.definitions)
        definitions["wizard_flat_table"]["formats"]["Revenue"]["precision"] = 3
        before = len(self.gallery.backend.writes)

        def final_inventory(*_):
            result = dict(entries)
            document = self.gallery.backend.documents[self.charts["wizard_flat_table"].id]["entry"]
            result[key] = SimpleNamespace(
                published_id=document["publishedId"], saved_id=document["savedId"]
            )
            return result

        with patch.object(commands, "context_for", return_value=self.gallery.context), patch.object(
            commands,
            "fetch",
            side_effect=lambda client, resource, unit, defs: self.gallery.datasets[
                resource.split(":")[1]
            ],
        ), patch.object(
            commands, "inventory", side_effect=final_inventory
        ), contextlib.redirect_stdout(io.StringIO()):
            commands.apply_changes(
                self.gallery.backend.client,
                {},
                entries,
                {key},
                {key: "local"},
                units,
                state,
                self.gallery.dataset_definitions,
                definitions,
                read_config("DL objects/dashboard.json"),
            )
        writes = self.gallery.backend.writes[before:]
        assert [name for name, _ in writes] == ["updateWizardChart"]
        assert writes[0][1]["chartId"] == self.charts["wizard_flat_table"].id

    def recovery_fixture(self, role):
        dashboard, _, _ = self.build_dashboard()
        units = configuration()
        state = EditState(
            {"target": "offline"}, path=Path(self.gallery.temporary.name) / "recovery.json"
        )

        def inventory(*_):
            entities = {"chart:" + key: value for key, value in self.charts.items()}
            entities["dashboard"] = dashboard
            result = {
                key: SimpleNamespace(
                    id=value.id,
                    saved_id=document["savedId"],
                    published_id=document["publishedId"],
                    key=document["key"],
                    is_locked=False,
                )
                for key, value in entities.items()
                for document in [self.gallery.backend.documents[value.id]["entry"]]
            }
            result.update(
                {
                    "dataset:" + role: SimpleNamespace(
                        id=dataset.id,
                        saved_id="dataset-revision",
                        published_id="dataset-revision",
                        key="Offline/" + role,
                        is_locked=False,
                    )
                    for role, dataset in self.gallery.datasets.items()
                }
            )
            return result

        entries = inventory()
        for key, entry in entries.items():
            state.remember(key, units[key], entry)
        definitions = copy.deepcopy(self.gallery.definitions)
        if role == "wizard_flat_table":
            definitions[role]["sort"] = [["Revenue", "asc"]]
        else:
            definitions[role]["description"] = "Recover an interrupted apply"
        key = "chart:" + role
        path = next(name for name in units[key]["files"] if name.endswith(".json"))
        units[key] = copy.deepcopy(units[key])
        units[key]["files"][path] = definitions[role]
        units[key]["fingerprint"] = fingerprint(units[key]["files"])

        def apply(current_state, current_entries):
            with patch.object(
                commands, "context_for", return_value=self.gallery.context
            ), patch.object(
                commands,
                "fetch",
                side_effect=lambda client, resource, unit, defs: self.gallery.datasets[
                    resource.split(":")[1]
                ],
            ), patch.object(commands, "inventory", side_effect=inventory), patch.object(
                commands, "check_queries"
            ), contextlib.redirect_stdout(io.StringIO()):
                commands.apply_changes(
                    self.gallery.backend.client,
                    {},
                    current_entries,
                    {key},
                    current_state.changes(units, current_entries),
                    units,
                    current_state,
                    self.gallery.dataset_definitions,
                    definitions,
                    read_config("DL objects/dashboard.json"),
                )

        return key, units, state, entries, definitions, inventory, apply

    def test_incremental_apply_resumes_its_saved_rebuild_after_fetch_failure(self):
        key, units, state, entries, definitions, inventory, apply = self.recovery_fixture(
            "wizard_flat_table"
        )
        self.gallery.backend.fail_after_operation = "updateWizardChart"
        with pytest.raises(RuntimeError, match="re-fetch"):
            apply(state, entries)
        restored = EditState({"target": "offline"}, path=state.path)
        entries = inventory()
        assert restored.owns_write(key, units[key], entries[key])
        assert entries[key].saved_id != entries[key].published_id
        self.gallery.context = self.gallery.new_context()
        apply(restored, entries)
        updated = self.gallery.backend.client.get.wizard_chart(
            by_id=entries[key].id, branch="published"
        )
        assert updated.saved_id == updated.published_id
        assert (
            check_chart(
                updated,
                context=self.gallery.context,
                datasets=self.gallery.datasets,
                definition=definitions["wizard_flat_table"],
            )
            == []
        )
        assert key not in restored.value["pending_apply"]
        assert self.gallery.context.resources.mutation_recorder is None

    def test_incremental_apply_resumes_its_published_write_without_repeating_it(self):
        for role, operation in (("ql_line", "updateQLChart"), ("js_table", "updateEditorChart")):
            with self.subTest(role=role):
                key, units, state, entries, _definitions, inventory, apply = self.recovery_fixture(
                    role
                )
                self.gallery.backend.fail_after_operation = operation
                with pytest.raises(RuntimeError, match="re-fetch"):
                    apply(state, entries)
                writes = len(self.gallery.backend.writes)
                restored = EditState({"target": "offline"}, path=state.path)
                entries = inventory()
                assert restored.changes(units, entries)[key] == "local"
                apply(restored, entries)
                assert len(self.gallery.backend.writes) == writes
                assert key not in restored.value["pending_apply"]

    def test_external_draft_after_interruption_is_not_adopted_as_recovery(self):
        key, units, state, entries, _definitions, inventory, apply = self.recovery_fixture(
            "wizard_flat_table"
        )
        self.gallery.backend.fail_after_operation = "updateWizardChart"
        with pytest.raises(RuntimeError):
            apply(state, entries)
        saved = self.gallery.backend.client.get.wizard_chart(by_id=entries[key].id, branch="saved")
        saved.update.description("External edit after interruption").mode("save").execute()
        writes = len(self.gallery.backend.writes)
        restored = EditState({"target": "offline"}, path=state.path)
        entries = inventory()
        assert not restored.owns_write(key, units[key], entries[key])
        with pytest.raises(DataLensUtilsError, match="unimported saved draft"):
            apply(restored, entries)
        assert len(self.gallery.backend.writes) == writes

    def test_selector_default_uses_point_update_without_removing_group(self):
        dashboard, tabs, contents = self.build_dashboard()
        contents = copy.deepcopy(contents)
        group = next(
            value
            for value in contents["wizard"]["selector_groups"].values()
            if "wizard_region_selector" in value["definitions"]
        )
        group["definitions"]["wizard_region_selector"]["control"]["default_value"] = ["East"]
        before = len(self.gallery.backend.writes)
        again = populate_dashboard(
            context=self.gallery.context,
            dashboard=dashboard,
            datasets=self.gallery.datasets,
            charts=self.charts,
            tab_definitions=tabs,
            contents=contents,
            chart_definitions=self.gallery.definitions,
            description="",
            hide_tabs=False,
        )
        writes = self.gallery.backend.writes[before:]
        assert [name for name, _ in writes] == ["updateDashboard"]
        assert dashboard.id == again.id
